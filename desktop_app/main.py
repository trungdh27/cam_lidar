import sys

from PySide6.QtWidgets import QApplication

from desktop_app.ui.main_window import MainWindow


def main():
    app = QApplication(sys.argv)

    app.setApplicationName(
        "Hardware Test Automation"
    )

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
