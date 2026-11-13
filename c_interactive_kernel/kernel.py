"""
Jupyter kernel "C (интерактивный)".

Based on jupyter-c-kernel 1.2.2 by Brendan Rius (MIT license): a cell is compiled
with gcc into a shared library, and resources/master.c loads it and calls main().
Added on top of it:
  - input for scanf: from the keyboard, from /*%input ... */ blocks, from files;
  - binary input: /*%bytes ... */ blocks (hex) and //%ints: lines (4-byte ints);
  - several inputs in one cell: the program runs once per input;
  - //%out: hex / ints / bytes to show binary output of fwrite; //%time to show run time;
  - /*%gdb ... */ block: run the cell under the gdb debugger;
  - a cell without main() that only defines things (functions, structs, globals, #include)
    is remembered and added to every later program, like a def cell in a Python notebook;
  - any other cell without main() is a debug cell: it works inside the last program before
    line //%line: N (default: the end of main); one cell may have several //%line parts.
    Statements are pasted into the program and run, lines that are bare expressions
    (N, Numbers[0:N], Items[0:N].Key, Point.X, *Head) are shown through gdb;
  - the Stop button stops the program, errors mark the cell as failed (Run All stops there).
"""

from queue import Queue
from threading import Thread

from ipykernel.kernelbase import Kernel
import fcntl
import json
import os
import re
import shutil
import signal
import struct
import subprocess
import tempfile
import termios
import time

START_MARK = b'\x02snippet{}\x02'  # a debug cell prints these around its output (with the number of its part),
END_MARK = b'\x03snippet\x03'  # so the program's own output can be dropped
SNIPPET = re.compile(rb'\x02snippet(\d+)\x02(.*?)(?=\x03snippet\x03|\x02snippet\d+\x02|\Z)', re.DOTALL)
SHOWN_PASSES = 20
RESOURCES = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'resources')
SIGNALS = {11: 'SIGSEGV, обращение к чужой памяти', 8: 'SIGFPE, деление на ноль', 6: 'SIGABRT, аварийная остановка'}
GDB_SIGNALS = {'SIGSEGV': SIGNALS[11], 'SIGFPE': SIGNALS[8], 'SIGABRT': SIGNALS[6]}
CONTROL = re.compile(r'(for|while|if|else|do|switch|case|default|return|break|continue|goto)\b')


class RealTimeSubprocess(subprocess.Popen):
    """A subprocess whose stdout and stderr are read in real time by two threads"""

    def __init__(self, cmd, write_to_stdout, write_to_stderr, stdin=None, cwd=None):
        self._write_to_stdout = write_to_stdout
        self._write_to_stderr = write_to_stderr

        super().__init__(cmd, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0, cwd=cwd)

        self._stdout_queue = Queue()
        self._stdout_thread = Thread(target=RealTimeSubprocess._enqueue_output, args=(self.stdout, self._stdout_queue))
        self._stdout_thread.daemon = True
        self._stdout_thread.start()

        self._stderr_queue = Queue()
        self._stderr_thread = Thread(target=RealTimeSubprocess._enqueue_output, args=(self.stderr, self._stderr_queue))
        self._stderr_thread.daemon = True
        self._stderr_thread.start()

    @staticmethod
    def _enqueue_output(stream, queue):
        for chunk in iter(lambda: stream.read(4096), b''):
            queue.put(chunk)
        stream.close()

    def write_contents(self):
        """Send everything read so far from stdout and stderr"""

        def read_all_from_queue(queue):
            result = b''
            for _ in range(queue.qsize()):
                result += queue.get_nowait()
            return result

        stdout_contents = read_all_from_queue(self._stdout_queue)
        if stdout_contents:
            self._write_to_stdout(stdout_contents)
        stderr_contents = read_all_from_queue(self._stderr_queue)
        if stderr_contents:
            self._write_to_stderr(stderr_contents)

    def finish(self):
        """Wait for the process and for both reader threads, then send the rest of the output"""
        self.wait()
        self._stdout_thread.join()
        self._stderr_thread.join()
        self.write_contents()


class CellFailed(Exception):
    """The cell did not work: compilation error, crash, Stop button. The cell is marked as failed"""


class CKernel(Kernel):
    implementation = 'c_interactive_kernel'
    implementation_version = '3.1'
    language = 'c'
    language_version = 'C17'
    language_info = {'name': 'c',
                     'mimetype': 'text/x-csrc',
                     'file_extension': '.c'}
    banner = "C kernel with input: gcc, scanf from the keyboard or /*%input*/ blocks, binary input, gdb, debug cells.\n"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.work = tempfile.mkdtemp(prefix='c_kernel_')  # cell.c, cell.so, input.txt and the gdb build live here
        self.master_path = os.path.join(self.work, 'master')
        subprocess.call(['gcc', os.path.join(RESOURCES, 'master.c'), '-std=c11', '-rdynamic', '-ldl', '-o', self.master_path])
        self.program = None  # the last cell with main(): its code and parsed settings, for debug cells
        self.definitions = {}  # cell id -> code of a cell with definitions only, added to every later program

    def do_shutdown(self, restart):
        shutil.rmtree(self.work, ignore_errors=True)
        return {'status': 'ok', 'restart': restart}

    # ---------- output to the notebook ----------

    def _write_to_stdout(self, contents):
        self.send_response(self.iopub_socket, 'stream', {'name': 'stdout', 'text': contents})

    def _write_to_stderr(self, contents):
        self.send_response(self.iopub_socket, 'stream', {'name': 'stderr', 'text': contents})

    def _start(self, cmd, stdin=None, collect=None, cwd=None):
        def write_stdout(contents):
            if collect is None:
                self._write_to_stdout(contents.decode('utf-8', 'replace'))
            else:
                collect.extend(contents)  # binary output is shown at the end, formatted
        return RealTimeSubprocess(cmd, write_stdout,
                                  lambda contents: self._write_to_stderr(contents.decode('utf-8', 'replace')),
                                  stdin, cwd)

    @staticmethod
    def _passes(texts, more=False):
        """Output of a debug cell that ran several times: one part per pass over the line"""
        if len(texts) == 1 and not more:
            return texts[0]
        text = ''
        for number, output in enumerate(texts[:SHOWN_PASSES]):
            text += '--- проход {} ---\n'.format(number + 1) + output
        if len(texts) > SHOWN_PASSES or more:
            text += '... дальше не показано, строка выполнялась больше {} раз\n'.format(SHOWN_PASSES)
        return text

    @staticmethod
    def _split_snippet(data):
        """What each part of the debug cell printed: part number -> texts, one per pass"""
        parts = {}
        for match in SNIPPET.finditer(bytes(data)):
            text = match.group(2).decode('utf-8', 'replace')
            parts.setdefault(int(match.group(1)), []).append(text if not text or text.endswith('\n') else text + '\n')
        return parts

    @staticmethod
    def _value_lines(shown):
        text = ''
        for label, value in shown:
            text += '{}: {}\n'.format(label, value[1:]) if value.startswith('!') else '{} = {}\n'.format(label, value)
        return text

    def _format_groups(self, groups, test, printed, result=None):
        """Output of a debug cell: for every //%line part its header, then what it printed and showed"""
        text = ''
        for index, group in enumerate(groups):
            own = printed.get(index, [])
            more = False
            if result is not None:
                passes = result['groups'][index]['passes']
                more = result['groups'][index]['more']
                own = [(own[number] if number < len(own) else '') + self._value_lines(shown) for number, shown in enumerate(passes)]
            text += '[строка {} программы, тест {}]\n'.format(group['number'], test)
            if own:
                text += self._passes(own, more)
            elif result is None or not result['crash']:
                text += '(эта строка ни разу не выполнилась)\n'
        return text

    @staticmethod
    def _format_output(data, mode, width):
        """Binary output: hex bytes, 4-byte little-endian ints or bytes with printable characters"""
        if not data:
            return '(пусто)\n'
        lines = []
        if mode == 'ints':
            whole = len(data) // 4 * 4
            numbers = struct.unpack('<{}i'.format(whole // 4), bytes(data[:whole]))
            for start in range(0, len(numbers), width or 8):
                lines.append(' '.join(str(number) for number in numbers[start:start + (width or 8)]))
            if whole < len(data):
                lines.append('лишние байты: ' + bytes(data[whole:]).hex(' ').upper())
        elif mode == 'bytes':
            for start in range(0, len(data), width or 16):
                lines.append(''.join(chr(byte) if 32 <= byte < 127 and byte != 92 else '\\x{:02x}'.format(byte)
                                     for byte in data[start:start + (width or 16)]))
        else:
            for start in range(0, len(data), width or 16):
                lines.append(bytes(data[start:start + (width or 16)]).hex(' ').upper())
        return '\n'.join(lines) + '\n'

    # ---------- reading the cell ----------

    def _parse(self, code):
        """Settings and inputs written in the cell as //%key: value lines and /*%name ... */ blocks"""
        cell = {'cflags': [], 'ldflags': [], 'args': [], 'out': 'text', 'width': 0, 'gdb': None,
                'lines': [], 'test': 1, 'time': False}
        inputs = []  # (position in the cell, bytes): the program runs once per input, in this order

        for block in re.finditer(r'/\*%(input|bytes|gdb)[ \t]*\n(.*?)\*/', code, re.DOTALL):
            kind, text = block.group(1), block.group(2)
            if kind == 'input':
                inputs.append((block.start(), text.encode()))
            elif kind == 'bytes':
                inputs.append((block.start(), bytes.fromhex(text)))  # fromhex skips spaces and line breaks
            else:
                cell['gdb'] = [line.strip() for line in text.splitlines() if line.strip()]

        for line in re.finditer(r'^//%\s*(\w+)\s*(?::(.*))?$', code, re.MULTILINE):
            key, value = line.group(1).lower(), (line.group(2) or '').strip()
            if key in ('cflags', 'ldflags'):
                cell[key] += value.split()
            elif key == 'args':
                cell['args'] += [argument.strip('"') for argument in re.findall(r'(?:[^\s,"]|"(?:\\.|[^"])*")+', value)]
            elif key == 'ints':
                inputs.append((line.start(), b''.join(struct.pack('<I', int(number, 0) % 2 ** 32) for number in value.split())))
            elif key == 'file':
                try:
                    with open(value, 'rb') as file:
                        inputs.append((line.start(), file.read()))
                except OSError as error:
                    self._write_to_stderr('[C kernel] cannot read input file: {}\n'.format(error))
            elif key == 'line':
                cell['lines'].append((code.count('\n', 0, line.start()), int(value)))  # (line of the cell, line of the program)
            elif key == 'test':
                cell['test'] = int(value)
            elif key == 'time':
                cell['time'] = True
            elif key == 'out':
                parts = value.lower().split()
                cell['out'] = parts[0] if parts else 'text'
                cell['width'] = int(parts[1]) if len(parts) > 1 else 0

        cell['inputs'] = [data for _, data in sorted(inputs, key=lambda item: item[0])]
        return cell

    @staticmethod
    def _without_comments(code):
        """The code with comments and string literals replaced by spaces; line breaks stay where they were"""
        return re.sub(r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'',
                      lambda match: re.sub(r'[^\n]', ' ', match.group(0)), code, flags=re.DOTALL)

    @staticmethod
    def _split_expressions(line):
        """N, Numbers[0:N] -> ['N', 'Numbers[0:N]']: commas outside brackets separate expressions"""
        parts, depth, current = [], 0, ''
        for char in line:
            if char in '([{':
                depth += 1
            elif char in ')]}':
                depth -= 1
            if char == ',' and depth == 0:
                parts.append(current.strip())
                current = ''
            else:
                current += char
        parts.append(current.strip())
        return [part for part in parts if part]

    @staticmethod
    def _to_gdb(expression):
        """Slices like in Python: A[2:5] -> A[2]@3, A[:N] -> A[0]@N; A[0:N].Key -> the field of every element"""
        match = re.match(r'^(.*?)\[([^\[\]:]*):([^\[\]:]+)\]((?:\.|->|\[).*)?$', expression)
        if match is None:
            return {'expression': expression}
        base, start, end, suffix = match.group(1), match.group(2).strip() or '0', match.group(3).strip(), match.group(4)
        if suffix:
            return {'base': base, 'start': start, 'end': end, 'suffix': suffix}
        return {'expression': '({0})[{1}]@(({2}) - ({1}))'.format(base, start, end)}

    def _classify(self, code, marks):
        """Split a debug cell into parts, one per //%line (marks: [(line of the cell, line of the program)]).
        In every part: statements (pasted into the program) and bare expressions (shown by gdb)"""
        clean = self._without_comments(code).split('\n')
        starts = dict(marks)
        groups = [{'line': None, 'statements': [], 'expressions': []}]
        depth, body_expected = 0, False
        for index, line in enumerate(clean):
            if index in starts:
                groups.append({'line': starts[index], 'statements': [], 'expressions': []})
            text = line.strip()
            if not text:
                continue
            if depth == 0 and not body_expected and not text.startswith('#') and not CONTROL.match(text) \
                    and text[-1] not in ';{}' and text.count('(') == text.count(')'):
                groups[-1]['expressions'] += self._split_expressions(code.split('\n')[index].strip() if '"' in text else text)
                continue
            groups[-1]['statements'].append(index)
            depth += text.count('{') - text.count('}')
            header = depth == 0 and CONTROL.match(text) is not None and text[-1] not in ';{}'
            body_expected = header or body_expected and text[-1] not in ';{}'
        lines = code.split('\n')
        for group in groups:  # only the part's own lines stay, the rest are blank: errors keep the cell's line numbers
            group['statements'] = '\n'.join(line if index in group['statements'] else '' for index, line in enumerate(lines))
        return [group for group in groups if group['expressions'] or group['statements'].strip()]

    # ---------- building the source ----------

    def _with_definitions(self, code, original=None, snippet=None):
        """Cells with definitions go before the program; #line keeps the cell's own line numbers.
        Each part is also saved under its own name, so gcc and gdb quote the lines the user sees"""
        parts = []
        self.files = {'cell.c': code if original is None else original}
        for number, definition in enumerate(self.definitions.values()):
            parts += ['#line 1 "defs{}.c"'.format(number + 1), definition]
            self.files['defs{}.c'.format(number + 1)] = definition
        if snippet is not None:
            self.files['snippet'] = snippet
        return '\n'.join(parts + ['#line 1 "cell.c"', code]) + '\n'

    @staticmethod
    def _paste(code, groups):
        """Paste the statements of every part into the program before its line; the lowest lines go first,
        so the line numbers above each place do not move"""
        lines = code.split('\n')
        for index in sorted(range(len(groups)), key=lambda index: (groups[index]['number'], index), reverse=True):
            if not CKernel._without_comments(groups[index]['statements']).strip():
                continue
            mark = START_MARK.replace(b'{}', str(index).encode())
            start = '{{ fflush(stdout); fwrite("{0}", 1, {1}, stdout);'.format(
                ''.join('\\x{:02x}'.format(byte) for byte in mark), len(mark))
            end = '; fflush(stdout); fwrite("{0}", 1, {1}, stdout); fflush(stdout); }}'.format(
                ''.join('\\x{:02x}'.format(byte) for byte in END_MARK), len(END_MARK))
            number = groups[index]['number']
            pasted = ['#line 1 "snippet"', start, '#line 1 "snippet"', groups[index]['statements'], end,
                      '#line {} "cell.c"'.format(number)]
            lines = lines[:number - 1] + pasted + lines[number - 1:]
        return '\n'.join(lines)

    @staticmethod
    def _default_line(code):
        """The end of main: before the last return and the free() calls right above it"""
        lines = code.split('\n')
        returns = [index + 1 for index, line in enumerate(lines) if re.match(r'\s*return\b', line)]
        number = returns[-1] if returns else len(lines)
        while number > 1 and re.match(r'\s*(free\s*\(.*|)$', lines[number - 2]):
            number -= 1
        while number < len(lines) and not lines[number - 1].strip():
            number += 1
        return number

    # ---------- running ----------

    @staticmethod
    def _waits_for_input(process):
        """True if the program is blocked in read() on stdin and nothing is left in the pipe"""
        try:
            with open('/proc/{}/syscall'.format(process.pid)) as file:
                fields = file.read().split()
        except OSError:
            return False
        if len(fields) < 2 or fields[0] != '0' or int(fields[1], 16) != 0:  # syscall 0 = read on x86_64, first argument = fd 0 (stdin)
            return False
        unread = fcntl.ioctl(process.stdin.fileno(), termios.FIONREAD, b'\0\0\0\0')
        return struct.unpack('i', unread)[0] == 0

    def _compile(self, cmd, source):
        for name, text in dict(self.files, **{'build.c': source}).items():
            with open(os.path.join(self.work, name), 'w') as file:
                file.write(text)
        process = self._start(cmd, cwd=self.work)
        try:
            while process.poll() is None:
                process.write_contents()
                time.sleep(0.005)
        except KeyboardInterrupt:
            process.kill()
            process.finish()
            raise CellFailed('остановлено')
        process.finish()
        if process.returncode != 0:
            self._write_to_stderr('[C kernel] ошибка компиляции, программа не запускалась\n')
            raise CellFailed('ошибка компиляции')

    def _build_library(self, cell, source, optimize='-O2'):
        self._compile(['gcc', 'build.c', '-fPIC', '-shared', '-rdynamic', optimize, '-Wall', '-Wno-unused-result']
                      + cell['cflags'] + ['-o', 'cell.so', '-lm'] + cell['ldflags'], source)

    def _build_executable(self, cell, source, warnings='-Wall'):
        self._compile(['gcc', '-g', '-O0', warnings, '-Wno-unused-result', '-fdebug-prefix-map={}=.'.format(self.work)]
                      + cell['cflags'] + ['build.c', '-o', 'cell', '-lm'] + cell['ldflags'], source)

    def _run(self, cell, given, allow_stdin):
        """Run the compiled cell once. given = bytes for stdin, or None to ask the keyboard"""
        collected = bytearray() if cell['out'] in ('hex', 'ints', 'bytes', 'snippet') else None
        process = self._start([self.master_path, os.path.join(self.work, 'cell.so')] + cell['args'], subprocess.PIPE, collected)
        began = time.perf_counter()
        typed = bytearray()  # what was typed on the keyboard, to repeat it if the program crashes

        if given is not None or not allow_stdin:
            writer = Thread(target=self._feed, args=(process, given or b''))  # a thread, so a big input does not block reading the output
            writer.daemon = True
            writer.start()

        try:
            while process.poll() is None:
                process.write_contents()
                if given is None and allow_stdin and not process.stdin.closed and self._waits_for_input(process):
                    time.sleep(0.05)
                    if process.poll() is not None or not self._waits_for_input(process):
                        continue
                    process.write_contents()
                    line = self.raw_input('')
                    try:
                        if line == 'EOF':
                            process.stdin.close()  # end of input for loops like while (scanf(...) == 1)
                        else:
                            typed.extend((line + '\n').encode())
                            process.stdin.write((line + '\n').encode())
                            process.stdin.flush()
                    except BrokenPipeError:
                        pass
                time.sleep(0.005)
        except (KeyboardInterrupt, EOFError):
            process.kill()
        process.finish()
        spent = time.perf_counter() - began

        if collected is not None:
            self._write_to_stdout(cell['format'](collected) if 'format' in cell else self._format_output(collected, cell['out'], cell['width']))
        if cell['time']:
            self._write_to_stdout('[время: {:.3f} с]\n'.format(spent))
        if process.returncode in (-signal.SIGINT, -signal.SIGKILL):
            self._write_to_stderr('[C kernel] программа остановлена\n')
            raise CellFailed('остановлено')
        if process.returncode < 0:  # killed by a signal, e.g. SIGSEGV = access to someone else's memory
            reason = SIGNALS.get(-process.returncode, 'сигнал {}'.format(-process.returncode))
            self._write_to_stderr('[C kernel] программа упала: {}\n'.format(reason))
            error = CellFailed(reason)
            error.crashed = True
            error.typed = bytes(typed) if given is None else given
            raise error
        if process.returncode != 0:
            self._write_to_stderr('[C kernel] main вернула {}\n'.format(process.returncode))

    @staticmethod
    def _feed(process, data):
        try:
            process.stdin.write(data)
            process.stdin.close()  # after the given input the program sees end of input (EOF)
        except (BrokenPipeError, ValueError):
            pass

    def _gdb(self, cmd, timeout=120):
        try:
            return subprocess.run(cmd, cwd=self.work, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, timeout=timeout).stdout.decode('utf-8', 'replace')
        except KeyboardInterrupt:
            raise CellFailed('остановлено')
        except subprocess.TimeoutExpired:
            self._write_to_stderr('[C kernel] gdb работал дольше {} с и был остановлен\n'.format(timeout))
            raise CellFailed('время вышло')

    def _need_gdb(self):
        if shutil.which('gdb') is None:
            self._write_to_stderr('[C kernel] gdb не установлен: sudo apt install gdb\n')
            raise CellFailed('нет gdb')

    def _write_input(self, given):
        with open(os.path.join(self.work, 'input.txt'), 'wb') as file:
            file.write(given or b'')

    def _debug(self, cell, code):
        """Compile with -g -O0 and run the commands of the /*%gdb*/ block in gdb"""
        self._need_gdb()
        self._build_executable(cell, self._with_definitions(code))
        self._write_input(cell['inputs'][0] if cell['inputs'] else b'')

        cmd = ['gdb', '-q', '-batch', '-iex', 'set debuginfod enabled off', '-ex', 'set style enabled off']
        for command in cell['gdb']:
            if re.match(r'(run|r|start)(\s|$)', command) and '<' not in command:
                command += ' < input.txt'  # the program reads the cell's input from the file
            cmd += ['-ex', command]
        text = self._gdb(cmd + ['--args', './cell'] + cell['args'])
        self._write_to_stdout('\n'.join(line for line in text.splitlines() if 'libthread_db' not in line) + '\n')

    def _show_values(self, run, groups, test, given):
        """Stop the program at the line of every part under gdb and show its expressions on every pass"""
        self._need_gdb()
        result = self._gdb_values(run, groups, given)
        try:
            with open(os.path.join(self.work, 'out.txt'), 'rb') as file:
                printed = self._split_snippet(file.read())
        except OSError:
            printed = {}
        self._write_to_stdout(self._format_groups(groups, test, printed, result))
        crash = result['crash']
        failed = [value for group in result['groups'] for shown in group['passes'] for _, value in shown if value.startswith('!')]
        if failed and not crash:
            raise CellFailed(failed[0][1:])
        if crash:
            reason = GDB_SIGNALS.get(crash['signal'], crash['signal'])
            self._write_to_stderr('[C kernel] программа упала: {}\n'.format(reason))
            if crash['values']:
                self._write_to_stdout('значения в момент падения:\n' + self._value_lines(crash['values']))
            self._write_to_stderr(self._describe_crash(crash))
            raise CellFailed(reason)

    def _gdb_values(self, run, groups, given):
        """Run values.py in gdb: stop at the line of every part, show its expressions; returns its result"""
        self._write_input(given)
        for name in ('out.txt', 'values_out.json'):
            if os.path.exists(os.path.join(self.work, name)):
                os.remove(os.path.join(self.work, name))
        with open(os.path.join(self.work, 'values_in.json'), 'w') as file:
            json.dump({'limit': SHOWN_PASSES, 'groups': [
                {'line': group['number'], 'expressions': [[expression, self._to_gdb(expression)] for expression in group['expressions']]}
                for group in groups]}, file)
        log = self._gdb(['gdb', '-q', '-batch', '-x', os.path.join(RESOURCES, 'values.py'), '--args', './cell'] + run['args'])
        try:
            with open(os.path.join(self.work, 'values_out.json')) as file:
                result = json.load(file)
        except (OSError, ValueError):
            self._write_to_stderr(log)
            raise CellFailed('gdb не справился')
        if result['error']:
            self._write_to_stderr('[C kernel] gdb: {}\n'.format(result['error']))
            raise CellFailed('gdb')
        return result

    def _describe_crash(self, crash):
        """Where the program fell, like a Python traceback: the chain of calls, then the local variables"""
        if not crash['stack']:
            return ''
        text = 'где упала (сначала самый внешний вызов):\n'
        for function, name, line in reversed(crash['stack']):
            source = self.files.get(name, '').split('\n')
            quoted = source[line - 1].strip() if 0 < line <= len(source) else ''
            place = 'строка {}'.format(line) if name == 'cell.c' else '{}, строка {}'.format(name, line)
            text += '  {}, {}:  {}\n'.format(function, place, quoted)
        if crash['locals']:
            text += 'переменные в {}:\n'.format(crash['stack'][0][0])
            for label, value in crash['locals']:
                text += '  {} = {}\n'.format(label, value.replace('\n', '\n  '))
        return text

    def _explain_crash(self, cell, code, given):
        """The program crashed: run it again under gdb with the same input to find the line"""
        try:
            self._build_executable(cell, self._with_definitions(code), '-w')
            result = self._gdb_values(cell, [], given)
        except CellFailed:
            return
        if result['crash']:
            self._write_to_stderr(self._describe_crash(result['crash']))

    def _debug_cell(self, code, cell, allow_stdin):
        """A cell without main(): works inside the last program before line //%line: N"""
        if self.program is None:
            self._write_to_stderr('[C kernel] в ячейке нет main: сначала запустите ячейку с программой, '
                                  'потом эта ячейка будет работать внутри неё\n')
            raise CellFailed('нет программы')
        program = self.program
        groups = self._classify(code, cell['lines'])
        for group in groups:
            group['number'] = group['line'] or self._default_line(program['code'])
        inputs = cell['inputs'] or program['cell']['inputs'] or [None]
        test = min(cell['test'], len(inputs))
        if any(self._without_comments(group['statements']).strip() for group in groups):
            source = self._with_definitions(self._paste(program['code'], groups), program['code'], code)
        else:  # only expressions: nothing is pasted, gdb just stops at the lines
            source = self._with_definitions(program['code'])
        if any(group['expressions'] for group in groups):
            run = dict(program['cell'], time=False)
            self._build_executable(run, source, '-w')
            self._show_values(run, groups, test, inputs[test - 1])
        else:
            run = dict(program['cell'], out='snippet', time=False,
                       format=lambda data: self._format_groups(groups, test, self._split_snippet(data)))
            self._build_library(run, source, '-O0')
            self._run(run, inputs[test - 1], allow_stdin)

    def _is_definitions(self, code):
        """True if the cell compiles on its own as a file of definitions: functions, structs, globals"""
        result = subprocess.run(['gcc', '-fsyntax-only', '-w', '-x', 'c', '-'], input=self._with_definitions(code).encode(),
                                cwd=self.work, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return result.returncode == 0

    def _execute(self, code, allow_stdin):
        cell = self._parse(code)

        if not self._without_comments(code).strip() and cell['gdb'] is not None:  # only a /*%gdb*/ block: debug the last program
            if self.program is None:
                self._write_to_stderr('[C kernel] сначала запустите ячейку с программой\n')
                raise CellFailed('нет программы')
            self._debug(dict(self.program['cell'], gdb=cell['gdb'], inputs=cell['inputs'] or self.program['cell']['inputs']),
                        self.program['code'])
            return
        if not self._without_comments(code).strip():
            return
        if not re.search(r'\bmain\s*\(', self._without_comments(code)):
            key = self.get_parent().get('metadata', {}).get('cellId') or code
            self.definitions.pop(key, None)  # the cell is run again: its old definitions are replaced
            if not cell['lines'] and self._is_definitions(code):
                self.definitions[key] = code
                self._write_to_stdout('[определения запомнены, они будут во всех следующих программах]\n')
            else:
                self._debug_cell(code, cell, allow_stdin)
            return

        self.program = {'code': code, 'cell': cell}
        if cell['gdb'] is not None:
            self._debug(cell, code)
            return

        self._build_library(cell, self._with_definitions(code))
        inputs = cell['inputs'] or [None]
        for number, given in enumerate(inputs):
            if len(inputs) > 1:
                self._write_to_stdout(('\n' if number else '') + '--- тест {} ---\n'.format(number + 1))
            try:
                self._run(cell, given, allow_stdin)
            except CellFailed as error:
                if getattr(error, 'crashed', False) and shutil.which('gdb'):
                    self._explain_crash(cell, code, error.typed)
                raise

    def do_execute(self, code, silent, store_history=True, user_expressions=None, allow_stdin=False):
        self._allow_stdin = allow_stdin  # ipykernel checks this flag in raw_input()
        reply = {'status': 'ok', 'execution_count': self.execution_count, 'payload': [], 'user_expressions': {}}
        if not code.strip():
            return reply
        ignored = signal.signal(signal.SIGINT, signal.default_int_handler)  # the Stop button sends SIGINT
        try:
            self._execute(code, allow_stdin)
        except (CellFailed, KeyboardInterrupt) as error:
            reply.update(status='error', ename='CellFailed', evalue=str(error) or 'остановлено', traceback=[])
        finally:
            signal.signal(signal.SIGINT, ignored)
        return reply
