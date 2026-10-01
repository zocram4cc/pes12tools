import sys

from PySide6.QtWidgets import QApplication

from main_window import MainWindow


def main():
    app = QApplication(sys.argv)
    debug_mode = "--debug" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    window = MainWindow(edit_path=args[0] if args else None, debug_mode=debug_mode)
    window.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
