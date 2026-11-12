"""Register the kernel in Jupyter for the current user: python -m c_interactive_kernel.install"""

import json
import os
import sys
import tempfile

from jupyter_client.kernelspec import KernelSpecManager

NAME = 'c-interactive'
SPEC = {
    'argv': [sys.executable, '-m', 'c_interactive_kernel', '-f', '{connection_file}'],
    'display_name': 'C (интерактивный)',
    'language': 'c',
}


def main():
    with tempfile.TemporaryDirectory() as folder:
        with open(os.path.join(folder, 'kernel.json'), 'w', encoding='utf-8') as file:
            json.dump(SPEC, file, ensure_ascii=False, indent=1)
        path = KernelSpecManager().install_kernel_spec(folder, NAME, user=True)
    print('ядро "{}" установлено в {}'.format(SPEC['display_name'], path))


if __name__ == '__main__':
    main()
