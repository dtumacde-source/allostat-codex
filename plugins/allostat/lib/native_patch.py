"""Small, fail-closed parser for the native apply_patch envelope, not shell."""
import re


def parse_native_patch(inputs):
    if not isinstance(inputs, dict) or len(inputs) != 1:
        raise ValueError('Ambiguous native patch input')
    key, text = next(iter(inputs.items()))
    if key not in {'command', 'input', 'patch'} or not isinstance(text, str):
        raise ValueError('Invalid native patch input')
    lines = text.splitlines()
    if not lines or lines[0] != '*** Begin Patch' or lines[-1] != '*** End Patch':
        raise ValueError('Incomplete native patch')
    operations, current = [], None
    for line in lines[1:-1]:
        header = re.fullmatch(r'\*\*\* (Add|Update|Delete) File: (.+)', line)
        if header:
            current = {'kind': header[1], 'targets': [header[2]], 'body': False, 'eof': False}
            if any(ord(c) < 32 for c in header[2]) or not header[2].strip():
                raise ValueError('Invalid patch target')
            operations.append(current)
        elif line.startswith('*** Move to: '):
            if current is None or current['kind'] != 'Update' or current['body'] or len(current['targets']) != 1:
                raise ValueError('Invalid patch move')
            target = line[len('*** Move to: '):]
            if not target.strip() or any(ord(c) < 32 for c in target):
                raise ValueError('Invalid patch target')
            current['targets'].append(target)
        elif current is None or current['kind'] == 'Delete' or current['eof']:
            raise ValueError('Invalid patch body')
        elif current['kind'] == 'Add':
            if not line.startswith('+'):
                raise ValueError('Invalid added content')
            current['body'] = True
        elif line == '*** End of File':
            current['eof'] = True
        elif line == '@@' or line.startswith('@@ ') or line.startswith((' ', '+', '-')):
            current['body'] = True
        else:
            raise ValueError('Invalid patch hunk')
    if not operations or any(o['kind'] != 'Delete' and not o['body'] for o in operations):
        raise ValueError('Empty patch operation')
    return operations
