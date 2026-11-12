# C (интерактивный): ядро Jupyter для языка Си

Пишете программу на Си в ячейке блокнота (VS Code или Jupyter) и запускаете её как в Python:

- ввод для `scanf`: с клавиатуры, из блока `/*%input ... */` в ячейке или из файла; несколько блоков дают несколько тестов;
- двоичный ввод и вывод (`fread`/`fwrite`): байты из условия задачи, числа;
- значения переменных без `printf`: ячейка `N, Numbers[0:N]` покажет их в последней программе, в любой строке;
- функции и структуры в отдельной ячейке, как `def` в Python;
- если программа упала, видно строку, цепочку вызовов и переменные;
- отладчик gdb прямо в ячейке, `//%time`, кнопка Stop.

Все возможности с примерами в [examples/c_interactive.ipynb](examples/c_interactive.ipynb).

Работает на **Linux** и на **Windows через WSL**. macOS не поддерживается.

## Установка на Linux

1. Поставьте [VS Code](https://code.visualstudio.com/) и в нём расширение **Jupyter**.
2. В терминале:

   ```bash
   git clone https://github.com/VysockovEgor/c-interactive-kernel.git ~/c-interactive-kernel
   ~/c-interactive-kernel/install.sh
   ```

   Скрипт сам поставит gcc, gdb и python, если их нет (спросит пароль), и зарегистрирует ядро.
3. Откройте `.ipynb`, справа вверху **Select Kernel** -> **Jupyter Kernel...** -> **C (интерактивный)**.

## Установка на Windows (через WSL)

Ядру нужен Linux, на Windows он встроен как WSL.

1. PowerShell **от имени администратора**: `wsl --install`, перезагрузите компьютер. Откроется Ubuntu и попросит придумать имя и пароль.
2. Поставьте [VS Code](https://code.visualstudio.com/) для Windows и расширения **WSL** и **Jupyter**.
3. В окне Ubuntu (меню Пуск -> Ubuntu):

   ```bash
   git clone https://github.com/VysockovEgor/c-interactive-kernel.git ~/c-interactive-kernel
   ~/c-interactive-kernel/install.sh
   ```

4. Там же перейдите в папку с блокнотами и откройте её в VS Code: `code .`
   (или в VS Code: F1 -> **WSL: Connect to WSL**, потом **Open Folder**). Внизу слева должно быть написано **WSL: Ubuntu**.
5. Если VS Code попросит, нажмите **Install in WSL** у расширения Jupyter. Дальше как на Linux: **Select Kernel** -> **C (интерактивный)**.

Файлы Windows видны из Ubuntu как `/mnt/c/...`, но блокноты лучше держать в домашней папке Ubuntu: там всё работает быстрее.

## Обновление и удаление

```bash
cd ~/c-interactive-kernel && git pull && ./install.sh
```

```bash
~/c-interactive-kernel/install.sh --uninstall
```

## Как это устроено

`install.sh` создаёт отдельное окружение Python в `~/.local/share/c-interactive-kernel` (системный Python не трогается), ставит туда пакет `c_interactive_kernel` и регистрирует ядро в `~/.local/share/jupyter/kernels/c-interactive`. Ячейка компилируется gcc в библиотеку и запускается; значения переменных и место падения показывает gdb (`c_interactive_kernel/resources/values.py`).

Основа: [jupyter-c-kernel](https://github.com/brendan-rius/jupyter-c-kernel) Брендана Риуса, лицензия MIT.
