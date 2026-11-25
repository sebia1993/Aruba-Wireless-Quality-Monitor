"""Current user's native editor and read-only controller observations."""
from datetime import datetime
import sys
import webbrowser

from .device_profiles import DeviceProfile
from .desktop_endpoint import dashboard_url
from .service_config import USER

if sys.platform == "win32":
    import commctrl as lv
    import pywintypes
    import win32api
    import win32con as c
    import win32gui as g
    from win32gui_struct import PackLVCOLUMN, PackLVITEM


class DeviceWindow:
    CLASS = "AWQM.DeviceSettings"
    SLOT, NEW, REMOVE, SETTINGS, RESULTS, ADVANCED = range(200, 206)
    (HOST, USERNAME, PASSWORD, REMEMBER, FINGERPRINT, KNOWN_HOSTS, BROWSE,
     TEST, SAVE, STOP, STATUS, OBSERVED, TABLE, GRID, OPEN_HTTPS) = range(210, 225)
    PREVIOUS, NEXT, PAGE, DETAILS, ADV_TEXT, ADV_URL = range(225, 231)
    TRUST_OPTIONS, DEFAULT_ACCOUNT = 231, 232
    HOST_LABEL, USER_LABEL, PASS_LABEL, KEY_HELP, FP_LABEL, FILE_LABEL, TABLE_LABEL = range(240, 247)
    SLOTS = tuple(f"WLC-{i}" for i in range(1, 5))
    PAGE_SIZE = 100

    def __init__(self, owner_hwnd, profiles, monitor, approved_url=None):
        self.owner = owner_hwnd
        self.profiles = profiles
        self.monitor = monitor
        self.approved_url = approved_url
        self.resolve_approved_url = lambda: self.approved_url
        self.on_profiles_changed = lambda: None
        self.hwnd = self.font = None
        self.instance = win32api.GetModuleHandle(None)
        self.registered = False
        self.controls = {}
        self.drafts = {}
        self.saved = {}
        self.default_username = ""
        self.draft_username = ""
        self.trust_expanded = False
        self.slot = self.SLOTS[0]
        self.view = self.SETTINGS
        self.notice = ""
        self.loading = False
        self.reuse_password = False
        self.table_names = ()
        self.table_name = None
        self.page = 0
        self.rendered = None
        self.columns = 0

    def _create(self):
        g.InitCommonControls()
        if not self.registered:
            cls = g.WNDCLASS()
            cls.hInstance = self.instance
            cls.lpszClassName = self.CLASS
            cls.lpfnWndProc = self._message
            cls.hCursor = g.LoadCursor(0, c.IDC_ARROW)
            cls.hbrBackground = c.COLOR_BTNFACE + 1
            g.RegisterClass(cls)
            self.registered = True
        left, top, right, bottom = win32api.GetMonitorInfo(
            win32api.MonitorFromWindow(self.owner, c.MONITOR_DEFAULTTONEAREST))["Work"]
        w, h = min(810, right-left), min(560, bottom-top)
        self.hwnd = g.CreateWindowEx(c.WS_EX_CONTROLPARENT, self.CLASS, "실제 장비 설정 및 조회 · AWQM",
            c.WS_OVERLAPPEDWINDOW | c.WS_CLIPCHILDREN, left + (right-left-w)//2,
            top + (bottom-top-h)//2, w, h, self.owner, 0, self.instance, None)
        if not self.font:
            desc = g.LOGFONT()
            desc.lfFaceName = "맑은 고딕"
            desc.lfHeight = -15
            self.font = g.CreateFontIndirect(desc)
        tab = c.WS_TABSTOP
        button = lambda id, text: self._child(id, "BUTTON", text, tab)
        label = lambda id, text: self._child(id, "STATIC", text, c.SS_NOPREFIX)
        edit = lambda id, extra=0: self._child(id, "EDIT", "", tab | c.ES_AUTOHSCROLL | extra, c.WS_EX_CLIENTEDGE)
        combo = lambda id: self._child(id, "COMBOBOX", "", tab | c.CBS_DROPDOWNLIST | c.WS_VSCROLL)
        combo(self.SLOT)
        button(self.NEW, "장비 추가")
        button(self.REMOVE, "삭제")
        for id, name in ((self.SETTINGS, "접속 설정"), (self.RESULTS, "실제 조회 결과"), (self.ADVANCED, "운영 대시보드")):
            self._child(id, "BUTTON", name, tab | c.BS_CHECKBOX | c.BS_PUSHLIKE)
        label(self.HOST_LABEL, "관리 IP  ·  SSH 포트 22")
        edit(self.HOST)
        label(self.USER_LABEL, "조회 계정")
        edit(self.USERNAME)
        button(self.DEFAULT_ACCOUNT, "새 장비 기본 계정으로")
        label(self.PASS_LABEL, "비밀번호")
        edit(self.PASSWORD, c.ES_PASSWORD)
        self._child(self.REMEMBER, "BUTTON", "Windows 계정에 비밀번호 저장", tab | c.BS_AUTOCHECKBOX)
        label(self.KEY_HELP, "")
        button(self.TRUST_OPTIONS, "SSH 키 고급 설정 펼치기")
        label(self.FP_LABEL, "SHA256 지문")
        edit(self.FINGERPRINT)
        label(self.FILE_LABEL, "known_hosts 파일")
        edit(self.KNOWN_HOSTS)
        button(self.BROWSE, "찾아보기…")
        button(self.TEST, "연결 테스트")
        button(self.SAVE, "저장하고 모니터링")
        label(self.DETAILS, "")
        label(self.TABLE_LABEL, "결과 표")
        combo(self.TABLE)
        self._child(self.GRID, "SysListView32", "", tab | lv.LVS_REPORT | lv.LVS_SHOWSELALWAYS, c.WS_EX_CLIENTEDGE)
        grid = self.controls[self.GRID]
        g.SendMessage(grid, lv.LVM_SETEXTENDEDLISTVIEWSTYLE, 0, lv.LVS_EX_FULLROWSELECT | lv.LVS_EX_GRIDLINES)
        button(self.PREVIOUS, "이전")
        button(self.NEXT, "다음")
        label(self.PAGE, "")
        button(self.STOP, "조회 중지")
        label(self.ADV_TEXT, "운영 대시보드에서 클러스터와 AP 앵커, WLC 메모리와 데이터 경로, 주변 Radio, "
              "단말 상세·이력과 품질 점수를 확인할 수 있습니다.\r\n\r\n"
              "표시되는 항목은 운영 계정의 역할, 현장 승인 설정과 장비의 지원 명령에 따릅니다.\r\n\r\n"
              "실제 단말 Airtime은 현 버전에서 지원하지 않습니다.")
        label(self.ADV_URL, "")
        button(self.OPEN_HTTPS, "운영 HTTPS 대시보드 열기")
        label(self.STATUS, "")
        label(self.OBSERVED, "")
        self.saved = {p.id: p for p in self.profiles.list()}
        self.default_username = self.profiles.default_username()
        self._select(next((id for id in self.SLOTS if id in self.saved), self.SLOTS[0]))

    def _child(self, id, kind, title, style, extended=0):
        hwnd = g.CreateWindowEx(extended, kind, title, c.WS_CHILD | style, 0, 0, 0, 0,
                                self.hwnd, id, self.instance, None)
        self.controls[id] = hwnd
        g.SendMessage(hwnd, c.WM_SETFONT, self.font, False)

    def show(self):
        if not self.hwnd:
            try:
                self._create()
            except Exception:
                self.close()
                raise
        g.ShowWindow(self.hwnd, c.SW_RESTORE if g.IsIconic(self.hwnd) else c.SW_SHOW)
        self.tick()
        try:
            g.SetForegroundWindow(self.hwnd)
        except pywintypes.error:
            g.FlashWindow(self.hwnd, True)
        if not g.IsChild(self.hwnd, g.GetFocus() or 0):
            g.SetFocus(self.controls[self.HOST if self.view == self.SETTINGS else self.SLOT])

    def close(self):
        if self.hwnd and g.IsWindow(self.hwnd):
            g.DestroyWindow(self.hwnd)
        if self.font:
            g.DeleteObject(self.font)
            self.font = None
        if self.registered:
            g.UnregisterClass(self.CLASS, self.instance)
            self.registered = False
        self.drafts.clear()

    def _text(self, id):
        return g.GetWindowText(self.controls[id])

    def _set(self, id, text):
        if self._text(id) != text:
            g.SetWindowText(self.controls[id], text)

    def _checked(self):
        return bool(g.SendMessage(self.controls[self.REMEMBER], c.BM_GETCHECK))

    def _fields(self):
        return tuple(self._text(id).strip() for id in (self.HOST, self.USERNAME, self.FINGERPRINT, self.KNOWN_HOSTS))

    def _store_draft(self):
        fields = self._fields()
        host, username, fingerprint, path = fields
        password, remember = self._text(self.PASSWORD), self._checked()
        suggested = self.default_username or self.draft_username
        if self.slot in self.saved or any((host, fingerprint, path, password, remember, username and username != suggested)):
            self.drafts[self.slot] = (fields, password, remember, self.reuse_password)
        else:
            self.drafts.pop(self.slot, None)
        if not suggested and USER.fullmatch(username):
            self.draft_username = username

    def _slots(self):
        combo = self.controls[self.SLOT]
        g.SendMessage(combo, c.CB_RESETCONTENT)
        for id in self.SLOTS:
            p = self.saved.get(id)
            title = p.host if p else "미등록"
            g.SendMessage(combo, c.CB_ADDSTRING, 0, f"{id}   ·   {title}")
        g.SendMessage(combo, c.CB_SETCURSEL, self.SLOTS.index(self.slot))
        g.EnableWindow(self.controls[self.NEW], len(self.saved) < len(self.SLOTS))
        g.EnableWindow(self.controls[self.REMOVE], self.slot in self.saved)

    def _select(self, id):
        self.slot = id
        self.notice = ""
        self.page = 0
        self.table_name = None
        self.table_names = ()
        self.rendered = None
        profile = self.saved.get(id)
        if id in self.drafts:
            fields, password, remember, reuse = self.drafts[id]
        elif profile:
            fields = (profile.host, profile.username, profile.fingerprint, profile.known_hosts)
            password, remember, reuse = "", self.profiles.remembered(profile), bool(self.profiles.password(profile))
        else:
            fields = ("", self.default_username or self.draft_username, "", "")
            password, remember, reuse = "", False, False
        self.trust_expanded = bool(fields[2] or fields[3])
        self.loading = True
        try:
            for control, value in zip((self.HOST, self.USERNAME, self.FINGERPRINT, self.KNOWN_HOSTS), fields):
                self._set(control, value)
            self._set(self.PASSWORD, password)
            g.SendMessage(self.controls[self.REMEMBER], c.BM_SETCHECK, int(remember), 0)
            self.reuse_password = reuse
            self._slots()
        finally:
            self.loading = False
        self._password_label()
        self._switch(self.view)

    def _password_label(self):
        self._set(self.PASS_LABEL, "비밀번호  ·  비워 두면 기존 비밀번호 사용" if self.reuse_password else "비밀번호")

    def _new(self):
        self._store_draft()
        available = [id for id in self.SLOTS if id not in self.saved and id not in self.drafts]
        if not available:
            self.notice = "등록할 장비를 위 목록에서 선택하세요. 장비는 최대 네 대까지 등록할 수 있습니다."
            return
        self.view = self.SETTINGS
        self._select(available[0])
        g.SetFocus(self.controls[self.HOST])

    def _remove(self):
        profile = self.saved.get(self.slot)
        if not profile or g.MessageBox(self.hwnd, f"{profile.id} ({profile.host})의 조회를 중지하고 등록 정보와 "
                "저장된 비밀번호를 삭제할까요?", "장비 삭제", c.MB_YESNO | c.MB_ICONQUESTION | c.MB_DEFBUTTON2) != c.IDYES:
            return
        self.profiles.remove(self.slot)
        self.monitor.forget(self.slot)
        del self.saved[self.slot]
        self.drafts.pop(self.slot, None)
        self.on_profiles_changed()
        self.view = self.SETTINGS
        self._select(next((id for id in self.SLOTS if id in self.saved), self.slot))

    def _start(self, once):
        profile = DeviceProfile(self.slot, *self._fields())
        password = self._text(self.PASSWORD)
        if not password and self.reuse_password and self.saved.get(self.slot) == profile:
            password = self.profiles.password(profile)
        if not password:
            g.SetFocus(self.controls[self.PASSWORD])
            raise ValueError("이 장비의 조회 계정 비밀번호를 입력하세요.")
        if not once:
            self.profiles.save(profile, password, self._checked())
            self.default_username = self.profiles.default_username()
            if self.saved.get(self.slot) != profile:
                self.monitor.forget(self.slot)
            self.saved[self.slot] = profile
            self.reuse_password = True
            self._set(self.PASSWORD, "")
            self._password_label()
            self._slots()
            self.on_profiles_changed()
        self.monitor.start(profile, password, once=once)
        if once and not self.default_username:
            self.default_username = self.profiles.set_default_username(profile.username)
        self.notice = ""
        self._switch(self.RESULTS)
        g.SetFocus(self.controls[self.TABLE])

    def _browse(self):
        try:
            path, _, _ = g.GetOpenFileNameW(hwndOwner=self.hwnd, Title="신뢰한 OpenSSH known_hosts 파일 선택",
                Filter="모든 파일\0*.*\0", Flags=c.OFN_EXPLORER | c.OFN_FILEMUSTEXIST | c.OFN_NOCHANGEDIR)
        except pywintypes.error as error:
            if error.winerror:
                self.notice = "파일을 열 수 없습니다. known_hosts 파일 경로를 직접 입력하세요."
            return
        self._set(self.KNOWN_HOSTS, path)

    def _approved(self):
        try:
            return dashboard_url(self.approved_url) if self.approved_url else None
        except ValueError:
            return None

    def _switch(self, view):
        self.view = view
        for nav in (self.SETTINGS, self.RESULTS, self.ADVANCED):
            g.SendMessage(self.controls[nav], c.BM_SETCHECK, int(nav == view), 0)
        self._layout()
        self._refresh()

    def _layout(self):
        if self.OBSERVED not in self.controls:
            return
        _, _, w, h = g.GetClientRect(self.hwnd)
        pad, gap = 16, 9
        inner = w - pad * 2
        half = (inner - gap)//2
        second = pad + half + gap
        boxes = {self.SLOT: (pad, 10, inner-174, 180), self.NEW: (w-pad-166, 10, 88, 29),
                 self.REMOVE: (w-pad-70, 10, 70, 29), self.STATUS: (pad, h-53, inner, 27),
                 self.OBSERVED: (pad, h-26, inner, 23)}
        for i, nav in enumerate((self.SETTINGS, self.RESULTS, self.ADVANCED)):
            boxes[nav] = (pad + 128*i, 48, 123, 30)
        if self.view == self.SETTINGS:
            username = self._text(self.USERNAME).strip()
            can_set_default = self.default_username and username != self.default_username and USER.fullmatch(username)
            self._set(self.USER_LABEL, "조회 계정" if can_set_default else "조회 계정  ·  새 장비에 자동 입력")
            self._set(self.KEY_HELP, "키 교체 시 관리자가 확인한 새 지문을 입력하고 저장하세요."
                      if self.trust_expanded else "첫 연결 시 SSH 키를 자동 등록하고, 키가 바뀌면 연결을 차단합니다.")
            self._set(self.TRUST_OPTIONS, "SSH 키 고급 설정 접기" if self.trust_expanded else "SSH 키 고급 설정 펼치기")
            if can_set_default:
                boxes[self.DEFAULT_ACCOUNT] = (w-pad-150, 81, 150, 24)
            if self.trust_expanded:
                boxes.update({self.FP_LABEL:(pad,264,122,23), self.FINGERPRINT:(pad+125,260,inner-125,27),
                              self.FILE_LABEL:(pad,298,122,23), self.KNOWN_HOSTS:(pad+125,294,inner-222,27),
                              self.BROWSE:(w-pad-90,293,90,29)})
            actions_y = 333 if self.trust_expanded else 264
            boxes.update({
                self.HOST_LABEL:(pad,85,half,20), self.USER_LABEL:(second,85,half-155 if can_set_default else half,20),
                self.HOST:(pad,106,half,27), self.USERNAME:(second,106,half,27),
                self.PASS_LABEL:(pad,138,inner,20), self.PASSWORD:(pad,159,half,27),
                self.REMEMBER:(second,158,half,28), self.KEY_HELP:(pad,187,inner,40),
                self.TRUST_OPTIONS:(pad,228,185,28), self.TEST:(pad,actions_y,122,32),
                self.SAVE:(pad+131,actions_y,164,32), self.STOP:(w-pad-99,actions_y,99,32)})
        elif self.view == self.RESULTS:
            bottom = h-93
            boxes.update({
                self.DETAILS:(pad,86,inner,63), self.TABLE_LABEL:(pad,162,57,24),
                self.TABLE:(pad+61,155,inner-185,180), self.STOP:(w-pad-111,154,111,29),
                self.GRID:(pad,193,inner,max(35,bottom-200)), self.PAGE:(pad,bottom+3,inner-160,23),
                self.PREVIOUS:(w-pad-150,bottom,72,28), self.NEXT:(w-pad-72,bottom,72,28)})
        else:
            boxes.update({self.ADV_TEXT:(pad,98,inner,158), self.ADV_URL:(pad,267,inner,46),
                          self.OPEN_HTTPS:(pad,317,224,32)})
        for id, hwnd in self.controls.items():
            if id in boxes:
                x, y, width, height = boxes[id]
                g.MoveWindow(hwnd, x, y, max(1,width), height, True)
                g.ShowWindow(hwnd, c.SW_SHOWNA)
            else:
                g.ShowWindow(hwnd, c.SW_HIDE)

    def _draw_table(self, tables):
        names = tuple(tables)
        combo = self.controls[self.TABLE]
        if names != self.table_names or self.table_name not in names:
            if self.table_name not in names:
                self.table_name = names[0] if names else None
                self.page = 0
            g.SendMessage(combo, c.CB_RESETCONTENT)
            for name in names:
                g.SendMessage(combo, c.CB_ADDSTRING, 0, name)
            if names:
                g.SendMessage(combo, c.CB_SETCURSEL, names.index(self.table_name))
            self.table_names = names
        labels, rows = tables.get(self.table_name, ((), ()))
        pages = max(1, (len(rows)+self.PAGE_SIZE-1)//self.PAGE_SIZE)
        self.page = min(self.page, pages-1)
        first = self.page*self.PAGE_SIZE
        current = rows[first:first+self.PAGE_SIZE]
        self._set(self.PAGE, f"총 {len(rows):,}개   ·   {self.page+1} / {pages} 페이지" if rows else "표시할 관측값이 없습니다.")
        g.EnableWindow(self.controls[self.PREVIOUS], self.page > 0)
        g.EnableWindow(self.controls[self.NEXT], self.page+1 < pages)
        snapshot = (self.table_name, labels, current)
        if self.rendered == snapshot:
            return
        grid = self.controls[self.GRID]
        g.SendMessage(grid, c.WM_SETREDRAW, False, 0)
        try:
            g.SendMessage(grid, lv.LVM_DELETEALLITEMS)
            for _ in range(self.columns):
                g.SendMessage(grid, lv.LVM_DELETECOLUMN, 0, 0)
            self.columns = len(labels)
            for i, label in enumerate(labels):
                data, keep = PackLVCOLUMN(text=label, cx=110, subItem=i)
                g.SendMessage(grid, lv.LVM_INSERTCOLUMNW, i, data)
            for r, row in enumerate(current):
                for column, value in enumerate(row):
                    data, keep = PackLVITEM(item=r, subItem=column, text=str(value))
                    message = lv.LVM_SETITEMTEXTW if column else lv.LVM_INSERTITEMW
                    g.SendMessage(grid, message, r if column else 0, data)
            for i in range(len(labels)):
                g.SendMessage(grid, lv.LVM_SETCOLUMNWIDTH, i, -1)
                width = g.SendMessage(grid, lv.LVM_GETCOLUMNWIDTH, i, 0)
                g.SendMessage(grid, lv.LVM_SETCOLUMNWIDTH, i, max(88, len(labels[i])*15+24, min(620, width+12)))
            self.rendered = snapshot
        finally:
            g.SendMessage(grid, c.WM_SETREDRAW, True, 0)
            g.InvalidateRect(grid, None, True)

    def _refresh(self):
        state = self.monitor.state(self.slot)
        g.EnableWindow(self.controls[self.STOP], bool(state.get("running")))
        try:
            current = DeviceProfile(self.slot, *self._fields())
        except ValueError:
            current = None
        other = state.get("profile") if state.get("running") and state.get("profile") != current else None
        if not current or state.get("profile") != current:
            state = {}
        status = f"기존 장비 {other.host} 조회 중 · 입력한 설정으로 아직 조회하지 않았습니다." if other else state.get("status") or "조회 대기"
        self._set(self.STATUS, self.notice or f"{self.slot}   ·   {status}")
        when = state.get("observed_at")
        timestamp = datetime.fromtimestamp(when).strftime("%Y-%m-%d %H:%M:%S") if when is not None else "아직 없음"
        historical = "   ·   이전 관측값" if state.get("stale") or (when and not state.get("running")) else ""
        self._set(self.OBSERVED, f"마지막 관측   {timestamp}{historical}")
        if self.view == self.RESULTS:
            self._set(self.DETAILS, "SSH 인증   " + (state.get("connection") or "대기") +
                      "\r\n명령 조회   " + (state.get("cli") or "대기") +
                      "\r\n갱신 주기   CPU·Radio 1분  /  활성 AP 2분  /  연결·BSS 5분  /  AP 목록 10분")
            self._draw_table(state.get("tables") or {})
        elif self.view == self.ADVANCED:
            url = self._approved()
            self._set(self.ADV_URL, url or "이 PC에 승인된 운영 HTTPS 주소가 설정되어 있지 않습니다.")
            g.EnableWindow(self.controls[self.OPEN_HTTPS], bool(url))

    def tick(self):
        if self.hwnd and g.IsWindowVisible(self.hwnd):
            self._refresh()

    def _command(self, id, notification):
        if self.loading:
            return
        if id in (self.HOST, self.USERNAME, self.FINGERPRINT, self.KNOWN_HOSTS):
            if notification == c.EN_CHANGE:
                self.reuse_password = False
                if id in (self.HOST, self.USERNAME):
                    self._set(self.PASSWORD, "")
                    g.SendMessage(self.controls[self.REMEMBER], c.BM_SETCHECK, 0, 0)
                self._password_label()
                if id == self.USERNAME:
                    self._layout()
                self.notice = ""
            return
        if id == self.SLOT and notification == c.CBN_SELCHANGE:
            index = g.SendMessage(self.controls[id], c.CB_GETCURSEL)
            if 0 <= index < len(self.SLOTS):
                self._store_draft()
                self._select(self.SLOTS[index])
        elif id == self.TABLE and notification == c.CBN_SELCHANGE:
            index = g.SendMessage(self.controls[id], c.CB_GETCURSEL)
            if 0 <= index < len(self.table_names):
                self.table_name = self.table_names[index]
                self.page = 0
        elif notification != c.BN_CLICKED:
            return
        elif id in (self.SETTINGS, self.RESULTS, self.ADVANCED):
            self.notice = ""
            self._switch(id)
        elif id == self.NEW:
            self._new()
        elif id == self.REMOVE:
            self._remove()
        elif id == self.BROWSE:
            self._browse()
        elif id == self.TRUST_OPTIONS:
            self.trust_expanded = not self.trust_expanded
            self._layout()
        elif id == self.DEFAULT_ACCOUNT:
            username = self._text(self.USERNAME).strip()
            self.default_username = self.profiles.set_default_username(username, replace=True)
            self.notice = "새 장비에 사용할 기본 조회 계정을 변경했습니다."
            g.SetFocus(self.controls[self.USERNAME])
            self._layout()
        elif id in (self.TEST, self.SAVE):
            self._start(once=id == self.TEST)
        elif id == self.STOP:
            self.notice = ""
            self.monitor.stop(self.slot)
        elif id in (self.PREVIOUS, self.NEXT):
            self.page = max(0, self.page + (1 if id == self.NEXT else -1))
        elif id == self.OPEN_HTTPS:
            self.approved_url = self.resolve_approved_url()
            url = self._approved()
            if not url:
                self.notice = "현재 승인된 운영 주소를 확인할 수 없습니다."
            elif not webbrowser.open(url):
                self.notice = "브라우저를 열 수 없습니다. 위의 운영 HTTPS 주소를 확인하세요."

    def _message(self, hwnd, msg, wparam, lparam):
        if msg == c.WM_COMMAND:
            id = wparam & 0xffff
            if id == c.IDCANCEL:
                g.ShowWindow(hwnd, c.SW_HIDE)
            elif self.OBSERVED in self.controls:
                try:
                    self._command(id, wparam >> 16)
                except ValueError as error:
                    self.notice = str(error)
                except (OSError, RuntimeError, pywintypes.error, webbrowser.Error):
                    self.notice = "요청을 완료하지 못했습니다. 장비 설정과 현재 계정의 파일 접근 권한을 확인하세요."
                self.tick()
            return 0
        if msg == c.WM_GETMINMAXINFO:
            # MINMAXINFO contains five POINTs. Allow use on a small laptop work area.
            import ctypes
            points = (ctypes.c_long * 10).from_address(lparam)
            points[6], points[7] = 540, 462
            return 0
        if msg == c.WM_SIZE:
            self._layout()
            return 0
        if msg == c.WM_CLOSE:
            g.ShowWindow(hwnd, c.SW_HIDE)
            return 0
        if msg == c.WM_DESTROY:
            self.controls.clear()
            self.hwnd = None
            self.columns = 0
            return 0
        return g.DefWindowProc(hwnd, msg, wparam, lparam)
