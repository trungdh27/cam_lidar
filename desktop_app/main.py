import sys

from PySide6.QtWidgets import QApplication

from desktop_app.ui.main_window import MainWindow
from desktop_app.ui.theme import APP_STYLE


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Hardware Test Automation")
    app.setStyleSheet(APP_STYLE)

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
