APP_STYLE = """
* {
    font-family: "DejaVu Sans", "Noto Sans", sans-serif;
    font-size: 13px;
    color: #172033;
}

QMainWindow, QWidget#AppRoot {
    background: #F6F8FC;
}

QFrame#Sidebar {
    background: #FFFFFF;
    border-right: 1px solid #DDE3EC;
}

QLabel#Brand {
    font-size: 16px;
    font-weight: 700;
    color: #182230;
    padding: 4px 10px 14px 10px;
}

QPushButton#NavButton {
    background: transparent;
    border: 0;
    border-radius: 7px;
    padding: 12px 14px;
    text-align: left;
    color: #344054;
    font-weight: 500;
}

QPushButton#NavButton:hover {
    background: #F2F6FC;
}

QPushButton#NavButton:checked {
    background: #EAF2FF;
    color: #155EEF;
    border: 1px solid #B2CCFF;
    font-weight: 700;
}

QFrame#Card {
    background: #FFFFFF;
    border: 1px solid #D8E0EB;
    border-radius: 9px;
}

QLabel#CardTitle {
    font-size: 14px;
    font-weight: 700;
    color: #182230;
}

QLabel#PageTitle {
    font-size: 25px;
    font-weight: 800;
    color: #111827;
}

QLabel#Muted {
    color: #667085;
}

QLabel#KeyLabel {
    color: #344054;
    font-weight: 500;
}

QLabel#ValueLabel {
    color: #182230;
    font-weight: 500;
}

QFrame#StatusChip {
    border-radius: 7px;
    padding: 1px;
}

QFrame#StatusChip[state="ok"] {
    background: #F0FDF4;
    border: 1px solid #ABEFC6;
}

QFrame#StatusChip[state="idle"] {
    background: #F8FAFC;
    border: 1px solid #D0D5DD;
}

QFrame#StatusChip[state="warning"] {
    background: #FFFAEB;
    border: 1px solid #FEDF89;
}

QFrame#StatusChip[state="error"] {
    background: #FEF3F2;
    border: 1px solid #FECDCA;
}

QLabel#ChipText {
    font-weight: 700;
}

QPushButton#PrimaryButton {
    background: #155EEF;
    border: 1px solid #155EEF;
    border-radius: 6px;
    color: white;
    padding: 9px 16px;
    font-weight: 700;
}

QPushButton#PrimaryButton:hover {
    background: #004EEB;
}

QPushButton#PrimaryButton:disabled {
    background: #B2CCFF;
    border-color: #B2CCFF;
}

QPushButton#OutlineButton {
    background: #FFFFFF;
    border: 1px solid #84ADFF;
    border-radius: 6px;
    color: #155EEF;
    padding: 9px 16px;
    font-weight: 700;
}

QPushButton#OutlineButton:hover {
    background: #EFF4FF;
}

QPushButton#DangerButton {
    background: #FFFFFF;
    border: 1px solid #F97066;
    border-radius: 6px;
    color: #D92D20;
    padding: 9px 16px;
    font-weight: 700;
}

QPushButton#SmallButton {
    background: #FFFFFF;
    border: 1px solid #D0D5DD;
    border-radius: 6px;
    padding: 6px 10px;
    color: #344054;
}

QLineEdit, QComboBox, QSpinBox {
    background: #FFFFFF;
    border: 1px solid #D0D5DD;
    border-radius: 6px;
    padding: 7px 9px;
    min-height: 20px;
}

QTableWidget {
    background: #FFFFFF;
    border: 1px solid #E4E7EC;
    border-radius: 6px;
    gridline-color: #EAECF0;
    alternate-background-color: #FCFCFD;
    selection-background-color: #EFF4FF;
    selection-color: #182230;
}

QHeaderView::section {
    background: #F8FAFC;
    color: #344054;
    border: 0;
    border-bottom: 1px solid #EAECF0;
    padding: 7px;
    font-weight: 700;
}

QTextEdit#LiveLog {
    background: #0E1117;
    color: #E6EDF3;
    border: 1px solid #202938;
    border-radius: 6px;
    font-family: "DejaVu Sans Mono", monospace;
    font-size: 12px;
    padding: 6px;
}

QDialog {
    background: #F8FAFC;
}
"""
