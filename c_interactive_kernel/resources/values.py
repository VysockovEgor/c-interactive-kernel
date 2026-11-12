"""
gdb script for debug cells: stops the program at a line and evaluates expressions there.

Run as: gdb -q -batch -x values.py --args ./cell ...
Reads values_in.json from the current directory:
  {"line": N, "expressions": [[label, gdb expression], ...], "limit": 20}
Writes values_out.json:
  {"passes": [[[label, text], ...], ...], "more": bool, "crash": null or {...}, "error": null or text}
The program reads input.txt and writes its output to out.txt.
"""

import json
import os

import gdb

with open('values_in.json') as file:
    settings = json.load(file)

for command in ('set pagination off', 'set confirm off', 'set print pretty on', 'set print elements 200',
                'set print null-stop on', 'set debuginfod enabled off', 'set style enabled off',
                'set startup-with-shell on'):
    try:
        gdb.execute(command, to_string=True)
    except gdb.error:
        pass

result = {'passes': [], 'more': False, 'crash': None, 'error': None}
state = {'stop': None, 'exited': False}


def on_stop(event):
    if isinstance(event, gdb.SignalEvent):
        state['stop'] = ('signal', event.stop_signal)
    elif isinstance(event, gdb.BreakpointEvent):
        state['stop'] = ('break', None)


def on_exit(event):
    state['exited'] = True


gdb.events.stop.connect(on_stop)
gdb.events.exited.connect(on_exit)


def translate(message):
    if message.startswith('No symbol "') and message.endswith('" in current context.'):
        return 'нет переменной {} в этом месте программы'.format(message[len('No symbol "'):-len('" in current context.')])
    if message.startswith('Cannot access memory at address'):
        return 'чужая память по адресу' + message[len('Cannot access memory at address'):]
    return message


def show(label, value):
    text = value.format_string(pretty_structs=True)
    kind = value.type.strip_typedefs()
    if kind.code == gdb.TYPE_CODE_PTR:
        target = kind.target().strip_typedefs()
        is_string = target.code == gdb.TYPE_CODE_INT and target.sizeof == 1
        if target.code != gdb.TYPE_CODE_FUNC and not is_string and int(value) != 0:
            text += '    (это адрес; элементы покажет срез, например {}[0:N])'.format(label)
    return text


def values():
    shown = []
    for label, expression in settings['expressions']:
        try:
            shown.append([label, show(label, gdb.parse_and_eval(expression))])
        except gdb.error as error:
            shown.append([label, '!' + translate(str(error))])
    return shown


def file_name(place):
    return os.path.basename(place.symtab.filename) if place.symtab is not None else ''


def is_user(place):
    return file_name(place) in ('cell.c', 'snippet') or file_name(place).startswith('defs')


def user_frame():
    """The innermost frame in the cell's own code (a crash may happen inside qsort, strlen and so on)"""
    frame = gdb.newest_frame()
    while frame is not None:
        place = frame.find_sal()
        if is_user(place):
            return frame, place
        frame = frame.older()
    return None, None


def stack():
    """[function, file, line] for every call in the cell's own code, innermost first"""
    calls = []
    frame = gdb.newest_frame()
    while frame is not None:
        place = frame.find_sal()
        if is_user(place):
            calls.append([frame.name() or '?', file_name(place), place.line])
        frame = frame.older()
    return calls


def local_values(frame):
    """Arguments and local variables of the frame, outer block first"""
    found, names, block = [], set(), frame.block()
    while block is not None:
        group = []
        for symbol in block:
            if (symbol.is_variable or symbol.is_argument) and symbol.name not in names:
                names.add(symbol.name)
                try:
                    group.append([symbol.name, show(symbol.name, symbol.value(frame))])
                except gdb.error as error:
                    group.append([symbol.name, translate(str(error))])
        found = group + found
        if block.function is not None:
            break
        block = block.superblock
    return found


try:
    if settings['line'] is not None:
        gdb.execute('break cell.c:{}'.format(settings['line']), to_string=True)
    command = 'run < input.txt > out.txt'
    while True:
        state['stop'] = None
        gdb.execute(command, to_string=True)
        command = 'continue'
        if state['exited'] or state['stop'] is None:
            break
        if state['stop'][0] == 'signal':
            frame, place = user_frame()
            crash = {'signal': state['stop'][1], 'file': None, 'line': None, 'function': None, 'values': [],
                     'stack': stack(), 'locals': []}
            if frame is not None:
                frame.select()
                crash.update(file=file_name(place), line=place.line, function=frame.name(), values=values(),
                             locals=local_values(frame))
            result['crash'] = crash
            break
        if len(result['passes']) >= settings['limit']:
            result['more'] = True
            break
        result['passes'].append(values())
except gdb.error as error:
    result['error'] = str(error)

with open('values_out.json', 'w') as file:
    json.dump(result, file, ensure_ascii=False)
