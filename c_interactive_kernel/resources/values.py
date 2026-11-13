"""
gdb script for debug cells: stops the program at lines and evaluates expressions there.

Run as: gdb -q -batch -x values.py --args ./cell ...
Reads values_in.json from the current directory:
  {"groups": [{"line": N, "expressions": [[label, spec], ...]}, ...], "limit": 20}
  spec is {"expression": gdb expression} or, for a slice with a field like A[0:N].Key,
  {"base": "A", "start": "0", "end": "N", "suffix": ".Key"}
Writes values_out.json:
  {"groups": [{"passes": [[[label, text], ...], ...], "more": bool}, ...], "crash": null or {...}, "error": null or text}
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

result = {'groups': [{'passes': [], 'more': False} for _ in settings['groups']], 'crash': None, 'error': None}
state = {'stop': None, 'exited': False}
SHOWN_ELEMENTS = 200


def on_stop(event):
    if isinstance(event, gdb.SignalEvent):
        state['stop'] = ('signal', event.stop_signal)
    elif isinstance(event, gdb.BreakpointEvent):
        state['stop'] = ('break', [breakpoint.number for breakpoint in event.breakpoints])


def on_exit(event):
    state['exited'] = True


gdb.events.stop.connect(on_stop)
gdb.events.exited.connect(on_exit)


def translate(message):
    if message.startswith('No symbol "') and message.endswith('" in current context.'):
        return 'нет переменной {} в этом месте программы'.format(message[len('No symbol "'):-len('" in current context.')])
    if message.startswith('There is no member named '):
        return 'в структуре нет поля ' + message[len('There is no member named '):].rstrip('.')
    if message.startswith('Cannot access memory at address'):
        return 'чужая память по адресу' + message[len('Cannot access memory at address'):]
    return message


def show(label, value):
    kind = value.type.strip_typedefs()
    text = value.format_string(pretty_structs=kind.code != gdb.TYPE_CODE_ARRAY)  # an array of structs fits in one line
    if kind.code == gdb.TYPE_CODE_PTR:
        target = kind.target().strip_typedefs()
        is_string = target.code == gdb.TYPE_CODE_INT and target.sizeof == 1
        if target.code != gdb.TYPE_CODE_FUNC and not is_string and int(value) != 0:
            text += '    (это адрес; элементы покажет срез, например {}[0:N])'.format(label)
    return text


def evaluate(label, spec):
    """The text of one expression; a slice with a field, A[0:N].Key, shows the field of every element"""
    if 'expression' in spec:
        return show(label, gdb.parse_and_eval(spec['expression']))
    count = int(gdb.parse_and_eval('({}) - ({})'.format(spec['end'], spec['start'])))
    parts = []
    for index in range(max(0, min(count, SHOWN_ELEMENTS))):
        element = gdb.parse_and_eval('({})[({}) + {}]{}'.format(spec['base'], spec['start'], index, spec['suffix']))
        parts.append(element.format_string(pretty_structs=False))
    return '{' + ', '.join(parts) + (', ...' if count > SHOWN_ELEMENTS else '') + '}'


def values(expressions, quiet=False):
    shown = []
    for label, spec in expressions:
        try:
            shown.append([label, evaluate(label, spec)])
        except gdb.error as error:
            if not quiet:
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
    owners = {}  # breakpoint number -> index of the group that stops there
    for index, group in enumerate(settings['groups']):
        owners[gdb.Breakpoint('cell.c:{}'.format(group['line'])).number] = index
    every_expression = []  # for the crash: every expression of the cell, each once
    for group in settings['groups']:
        every_expression += [item for item in group['expressions'] if item[0] not in [known[0] for known in every_expression]]
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
                crash.update(file=file_name(place), line=place.line, function=frame.name(),
                             values=values(every_expression, quiet=True), locals=local_values(frame))
            result['crash'] = crash
            break
        for number in state['stop'][1]:
            if number not in owners:
                continue
            group = result['groups'][owners[number]]
            if len(group['passes']) >= settings['limit']:
                group['more'] = True
                gdb.breakpoints()[[breakpoint.number for breakpoint in gdb.breakpoints()].index(number)].enabled = False
            else:
                group['passes'].append(values(settings['groups'][owners[number]]['expressions']))
        if not any(breakpoint.enabled for breakpoint in gdb.breakpoints()):
            break
except gdb.error as error:
    result['error'] = str(error)

with open('values_out.json', 'w') as file:
    json.dump(result, file, ensure_ascii=False)
