STRESS_CHART_COLORS = {
    "blue": "#155EEF",
    "orange": "#F79009",
    "red": "#D92D20",
    "purple": "#7F56D9",
    "green": "#039855",
}


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

QFrame#SectionFrame {
    background: #FFFFFF;
    border: 1px solid #D8E0EB;
    border-radius: 9px;
}

QFrame#SectionFrame[semantic="primary"] {
    border: 2px solid #4C8DFF;
}

QFrame#SectionFrame[semantic="info"] {
    border: 2px solid #84ADFF;
}

QFrame#SectionFrame[semantic="purple"] {
    border: 2px solid #B692F6;
}

QFrame#SectionFrame[semantic="teal"] {
    border: 2px solid #4DB6AC;
}

QFrame#SectionFrame[semantic="neutral"] {
    border: 1px solid #D8E0EB;
}

QFrame#SectionFrame[semantic="active"] {
    border: 2px solid #84ADFF;
}

QFrame#SectionFrame[semantic="pass"] {
    border: 2px solid #56B870;
}

QFrame#SectionFrame[semantic="warning"] {
    border: 2px solid #E7A11A;
}

QFrame#SectionFrame[semantic="error"] {
    border: 2px solid #D92D20;
}

QFrame#SectionFrame[semantic="console"] {
    background: #0E1117;
    border: 2px solid #344054;
}

QFrame#SectionFrame[semantic="console"] QLabel {
    color: #E6EDF3;
}

QFrame#Card[accent="blue"] {
    border-top: 3px solid #84ADFF;
}

QFrame#Card[accent="purple"] {
    border-top: 3px solid #B692F6;
}

QWidget#AudioHeader {
    background: transparent;
}

QFrame#AudioEvidenceBar {
    background: #FFFFFF;
    border: 1px solid #D8E0EB;
    border-radius: 7px;
}

QScrollArea#AudioScrollArea {
    background: transparent;
}

QWidget#AudioPage QComboBox,
QWidget#AudioPage QLineEdit,
QWidget#AudioPage QSpinBox {
    min-height: 24px;
}

QWidget#AudioPage QPushButton#SmallButton,
QWidget#AudioPage QToolButton#DetailsToggle {
    min-height: 28px;
}

QLabel#AudioValueEmphasis {
    color: #155EEF;
    font-size: 15px;
    font-weight: 800;
    min-width: 42px;
}

QLabel#WarningLabel {
    color: #B54708;
    font-weight: 600;
}

QTabWidget#AudioAdvancedTabs::pane {
    border: 1px solid #E4E7EC;
    background: #FCFCFD;
}

QTabWidget#AudioAdvancedTabs QTabBar::tab {
    background: #F8FAFC;
    border: 1px solid #E4E7EC;
    padding: 5px 10px;
    color: #475467;
    font-weight: 600;
}

QTabWidget#AudioAdvancedTabs QTabBar::tab:selected {
    background: #FFFFFF;
    color: #155EEF;
    border-bottom: 2px solid #155EEF;
}

QTabWidget#AudioTestTabs::pane {
    border: 1px solid #D8E0EB;
    background: #FFFFFF;
}

QTabWidget#AudioTestTabs QTabBar::tab {
    background: #EEF2F7;
    border: 1px solid #D8E0EB;
    padding: 6px 11px;
    color: #475467;
    font-weight: 600;
}

QTabWidget#AudioTestTabs QTabBar::tab:selected {
    background: #FFFFFF;
    color: #155EEF;
    border-bottom: 2px solid #155EEF;
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

QPushButton#PrimaryButton:pressed {
    background: #00359E;
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

QPushButton#OutlineButton:checked {
    background: #EAF2FF;
    border-color: #155EEF;
    color: #155EEF;
}

QPushButton#OutlineButton:disabled {
    background: #F2F4F7;
    border-color: #D0D5DD;
    color: #98A2B3;
}

QPushButton#DangerButton {
    background: #D92D20;
    border: 1px solid #D92D20;
    border-radius: 6px;
    color: #FFFFFF;
    padding: 9px 16px;
    font-weight: 700;
}

QPushButton#DangerButton:hover { background: #B42318; }
QPushButton#DangerButton:pressed { background: #912018; }
QPushButton#DangerButton:disabled { background: #FECDCA; border-color: #FECDCA; color: #912018; }

QPushButton#SuccessButton {
    background: #067647;
    border: 1px solid #067647;
    border-radius: 6px;
    color: #FFFFFF;
    padding: 9px 16px;
    font-weight: 700;
}

QPushButton#SuccessButton:hover { background: #05603A; }
QPushButton#SuccessButton:pressed { background: #054F31; }
QPushButton#SuccessButton:disabled { background: #ABEFC6; border-color: #ABEFC6; color: #085D3A; }

QPushButton#WarningButton {
    background: #F79009;
    border: 1px solid #F79009;
    border-radius: 6px;
    color: #FFFFFF;
    padding: 9px 16px;
    font-weight: 700;
}

QPushButton#WarningButton:hover {
    background: #DC6803;
}

QPushButton#WarningButton:pressed { background: #B54708; }
QPushButton#WarningButton:disabled { background: #FEDF89; border-color: #FEDF89; color: #93370D; }

QPushButton#SmallButton, QToolButton#SmallButton {
    background: #FFFFFF;
    border: 1px solid #D0D5DD;
    border-radius: 6px;
    padding: 6px 10px;
    color: #344054;
}

QPushButton#SmallButton:checked {
    background: #EAF2FF;
    border-color: #155EEF;
    color: #175CD3;
    font-weight: 700;
}

QPushButton#SmallButton:hover, QToolButton#SmallButton:hover { background: #F2F4F7; border-color: #98A2B3; }
QPushButton#SmallButton:pressed, QToolButton#SmallButton:pressed { background: #EAECF0; }

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

QTabWidget#StressTabs::pane, QTabWidget#StressDetailTabs::pane {
    border: 1px solid #D8E0EB;
    background: #FFFFFF;
}

QTabWidget#StressTabs QTabBar::tab, QTabWidget#StressDetailTabs QTabBar::tab {
    background: #EEF2F7;
    border: 1px solid #D8E0EB;
    padding: 6px 11px;
    color: #475467;
    font-weight: 600;
}

QTabWidget#StressTabs QTabBar::tab:selected, QTabWidget#StressDetailTabs QTabBar::tab:selected {
    background: #FFFFFF;
    color: #155EEF;
    border-bottom: 2px solid #155EEF;
}

QFrame#StressPanel, QFrame#StressFilterBar {
    background: #FFFFFF;
    border: 1px solid #D8E0EB;
    border-radius: 8px;
}

QWidget#StressExecutionView, QWidget#StressExecutionContent,
QScrollArea#StressExecutionScroll, QScrollArea#StressExecutionScroll > QWidget > QWidget {
    background: #F6F8FC;
}

QFrame#ActiveTestCard, QFrame#CaseMonitorPanel, QFrame#ExecutionActionBar {
    background: #FFFFFF;
    border: 1px solid #D8E0EB;
    border-radius: 10px;
}

QFrame#ExecutionActionBar {
    border-color: #C9D7EC;
}

QLabel#ActiveTestIcon {
    background: #EAF2FF;
    border: 1px solid #B2CCFF;
    border-radius: 9px;
    color: #155EEF;
    font-size: 24px;
    font-weight: 800;
}

QLabel#ActiveTestTitle {
    color: #101828;
    font-size: 18px;
    font-weight: 800;
}

QLabel#SectionTitle {
    color: #182230;
    font-size: 15px;
    font-weight: 750;
}

QLabel#MetricCaption, QLabel#DashboardMetricLabel {
    color: #667085;
    font-size: 11px;
}

QLabel#ActiveSummaryValue {
    color: #182230;
    font-size: 15px;
    font-weight: 800;
}

QFrame#DashboardSeparator {
    color: #E4E7EC;
    max-width: 1px;
}

QLabel#DomainBadge {
    color: #175CD3;
    background: #EFF8FF;
    border: 1px solid #B2DDFF;
    border-radius: 8px;
    padding: 2px 7px;
    font-size: 10px;
    font-weight: 700;
}

QFrame#CaseMetricField {
    border-right: 1px solid #EAECF0;
}

QLabel#CaseMetricValue {
    color: #182230;
    font-size: 13px;
    font-weight: 750;
}

QFrame#MetricDashboardCard {
    background: #FFFFFF;
    border: 1px solid #E4E7EC;
    border-radius: 8px;
}

QFrame#MetricDashboardCard[relevant="true"] {
    background: #F8FBFF;
    border-color: #B2CCFF;
}

QFrame#MetricDashboardCard[state="ok"] { border-color: #ABEFC6; }
QFrame#MetricDashboardCard[state="warning"] { border-color: #FEDF89; }
QFrame#MetricDashboardCard[state="error"] { border-color: #FECDCA; }
QFrame#MetricDashboardCard[state="suspect"] { border-color: #D6BBFB; }

QLabel#DashboardCardTitle {
    color: #344054;
    font-size: 12px;
    font-weight: 800;
}

QLabel#DashboardMetricValue {
    color: #182230;
    font-size: 11px;
    font-weight: 700;
}

QPushButton#SegmentButton {
    background: #FFFFFF;
    border: 1px solid #D0D5DD;
    color: #475467;
    padding: 5px 9px;
    font-size: 11px;
    font-weight: 650;
}

QPushButton#SegmentButton:checked {
    background: #EAF2FF;
    border-color: #84ADFF;
    color: #155EEF;
}

QTableWidget#ExecutionQueueTable {
    border: 0;
    border-radius: 0;
}

QTableWidget#ExecutionQueueTable QProgressBar {
    min-height: 14px;
    max-height: 14px;
    color: #344054;
    font-size: 10px;
    text-align: center;
}

QFrame#HeaderSeparator {
    color: #D8E0EB;
    max-width: 1px;
}

QToolButton#HeaderFolderButton, QToolButton#DetailsToggle {
    background: #FFFFFF;
    border: 1px solid #D0D5DD;
    border-radius: 6px;
    color: #344054;
    padding: 4px 6px;
}

QToolButton#HeaderFolderButton:hover, QToolButton#DetailsToggle:hover {
    background: #EFF4FF;
    border-color: #84ADFF;
    color: #155EEF;
}

QTableWidget#StressTable {
    selection-background-color: #EAF2FF;
    selection-color: #101828;
}

QTableWidget#StressTable::item {
    padding: 2px 4px;
}

QLabel#StatusBadge {
    border-radius: 9px;
    padding: 3px 8px;
    font-size: 11px;
    font-weight: 700;
}

QLabel#StatusBadge[tone="success"] {
    color: #067647;
    background: #ECFDF3;
    border: 1px solid #ABEFC6;
}

QLabel#StatusBadge[tone="error"] {
    color: #B42318;
    background: #FEF3F2;
    border: 1px solid #FECDCA;
}

QLabel#StatusBadge[tone="warning"] {
    color: #B54708;
    background: #FFFAEB;
    border: 1px solid #FEDF89;
}

QLabel#StatusBadge[tone="running"] {
    color: #175CD3;
    background: #EFF8FF;
    border: 1px solid #B2DDFF;
}

QLabel#StatusBadge[tone="prepared"] {
    color: #175CD3;
    background: #EFF8FF;
    border: 1px solid #B2DDFF;
}

QLabel#StatusBadge[tone="neutral"], QLabel#StatusBadge[tone="stopped"] {
    color: #475467;
    background: #F2F4F7;
    border: 1px solid #D0D5DD;
}

QTextBrowser#StressRichText {
    background: #FFFFFF;
    border: 0;
    padding: 6px;
}

QTextEdit#LiveLog, QPlainTextEdit#LiveLog {
    background: #0E1117;
    color: #E6EDF3;
    border: 1px solid #202938;
    border-radius: 6px;
    font-family: "DejaVu Sans Mono", monospace;
    font-size: 12px;
    padding: 6px;
}

QPlainTextEdit#AudioExecutionLog {
    background: #0E1117;
    color: #E6EDF3;
    border: 1px solid #344054;
    border-radius: 6px;
    padding: 8px;
    selection-background-color: #264F78;
}

QDialog {
    background: #F8FAFC;
}

QFrame#MetricCard {
    background: #FFFFFF;
    border: 1px solid #D8E0EB;
    border-radius: 9px;
}

QFrame#WifiMetricCard, QFrame#WifiMiniMonitor {
    background: #FFFFFF;
    border: 1px solid #D8E0EB;
    border-radius: 7px;
}

QLabel#WifiMetricValue {
    color: #182230;
    font-size: 18px;
    font-weight: 700;
}

QPlainTextEdit#WifiCommand {
    background: #F2F4F7;
    border: 1px solid #D8E0EB;
    border-radius: 4px;
    font-family: "DejaVu Sans Mono", monospace;
}

QLabel#MetricLabel {
    color: #667085;
    font-weight: 600;
}

QLabel#MetricValue {
    color: #182230;
    font-size: 26px;
    font-weight: 800;
}

QLabel#MetricNote {
    color: #667085;
    font-size: 11px;
}

QLabel#ReadinessValue {
    color: #155EEF;
    font-size: 26px;
    font-weight: 800;
}

QProgressBar {
    background: #EAECF0;
    border: 0;
    border-radius: 5px;
    min-height: 10px;
    max-height: 10px;
}

QProgressBar::chunk {
    background: #155EEF;
    border-radius: 5px;
}

"""
