import Foundation
import AppKit

/// Persisted display policy controlling how the floating HUD links with ChatGPT Work.
enum HudDisplayMode: String, CaseIterable {
    case menuBarOnly
    case projectLinked
    case chatgptAlways
    case alwaysOnTop

    static let defaultsKey = "xy_hud_display_mode"

    var menuTitle: String {
        switch self {
        case .menuBarOnly:
            return "仅菜单栏图标（推荐·浮窗关闭）"
        case .projectLinked:
            return "随 ChatGPT 课题自动显隐"
        case .chatgptAlways:
            return "随 ChatGPT 打开即显示"
        case .alwaysOnTop:
            return "始终全局置顶"
        }
    }
}

/// Application delegate owning the macOS menu bar status item, ChatGPT Work
/// auto-link engine, BackendSupervisor lifecycle, dynamic TTY approvals submenu,
/// and the floating Cowork-style HUD panel controller.
final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    private static let chatGPTBundleID = "com.openai.codex"
    private static let migrationKey = "xy_hud_migrated_menubar_only_v2"
    private static let microPollInterval: TimeInterval = 2.5
    private static let recentSessionThresholdSeconds: TimeInterval = 30.0 * 60.0
    private static let activeSessionStatuses: Set<String> = ["running", "blocked", "stalled"]

    private var statusItem: NSStatusItem?
    private var mainMenu: NSMenu?
    private var linkStatusMenuItem: NSMenuItem?
    private var modeMenuItems: [HudDisplayMode: NSMenuItem] = [:]
    private var toggleVisibilityMenuItem: NSMenuItem?
    private var toggleModeMenuItem: NSMenuItem?
    private var approvalsParentItem: NSMenuItem?
    private var approvalsSubmenu: NSMenu?

    private var hudController: HudPanelController?
    private let backendSupervisor = BackendSupervisor()
    private var cachedApprovals: [ApprovalCommandItem] = []

    private var displayMode: HudDisplayMode = .menuBarOnly
    private var microPollTimer: Timer?
    private var isPollingMicro: Bool = false

    // Detected ChatGPT project-conversation state from `/api/micro`
    private var hasActiveProjectConversation: Bool = false
    private var detectedProjectId: String?
    private var detectedProjectName: String?
    private var detectedSessionId: String?

    // Respect explicit user hide until user manually re-shows or enters a new project session
    private var manuallyHidden: Bool = false
    private var manuallyHiddenForSessionId: String?
    // Temporary manual reveal while in `.menuBarOnly` mode (never auto-shown)
    private var temporarilyShownInMenuBarOnly: Bool = false

    private lazy var isoFormatterFractional: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()

    private lazy var isoFormatterPlain: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime]
        return f
    }()

    func applicationDidFinishLaunching(_ notification: Notification) {
        let defaults = UserDefaults.standard
        let migrated = defaults.bool(forKey: Self.migrationKey)
        let savedRaw = defaults.string(forKey: HudDisplayMode.defaultsKey)

        if !migrated {
            if savedRaw == nil || savedRaw == HudDisplayMode.projectLinked.rawValue {
                displayMode = .menuBarOnly
                defaults.set(HudDisplayMode.menuBarOnly.rawValue, forKey: HudDisplayMode.defaultsKey)
            } else if let savedRaw = savedRaw, let savedMode = HudDisplayMode(rawValue: savedRaw) {
                displayMode = savedMode
            } else {
                displayMode = .menuBarOnly
                defaults.set(HudDisplayMode.menuBarOnly.rawValue, forKey: HudDisplayMode.defaultsKey)
            }
            defaults.set(true, forKey: Self.migrationKey)
        } else if let savedRaw = savedRaw, let savedMode = HudDisplayMode(rawValue: savedRaw) {
            displayMode = savedMode
        } else {
            displayMode = .menuBarOnly
            defaults.set(HudDisplayMode.menuBarOnly.rawValue, forKey: HudDisplayMode.defaultsKey)
        }

        let controller = HudPanelController()
        controller.onStateChange = { [weak self] in
            self?.syncMenuState()
        }
        controller.onUserRequestedHide = { [weak self] in
            guard let self = self else { return }
            self.manuallyHidden = true
            self.manuallyHiddenForSessionId = self.detectedSessionId
            self.temporarilyShownInMenuBarOnly = false
            self.syncMenuState()
        }
        self.hudController = controller

        backendSupervisor.onBackendBecameHealthy = { [weak self] in
            guard let self = self else { return }
            if self.displayMode != .menuBarOnly || self.hudController?.isVisible == true {
                self.hudController?.reload()
            }
            if self.displayMode == .projectLinked && self.isChatGPTRunning() {
                self.pollMicroOnce()
            }
            self.refreshApprovalsSubmenu()
            self.syncMenuState()
        }
        backendSupervisor.onStatusChanged = { [weak self] in
            self?.syncMenuState()
        }

        setupStatusBar()
        observeWorkspaceNotifications()

        NotificationCenter.default.addObserver(
            self,
            selector: #selector(handleScreenChange),
            name: NSApplication.didChangeScreenParametersNotification,
            object: nil
        )

        bootstrapInitialLinkState()
    }

    func applicationWillTerminate(_ notification: Notification) {
        stopMicroPolling()
        NSWorkspace.shared.notificationCenter.removeObserver(self)
        NotificationCenter.default.removeObserver(self)
        backendSupervisor.stopOwnedBackend(synchronous: true)
    }

    // MARK: - Workspace Linkage

    private func observeWorkspaceNotifications() {
        let nc = NSWorkspace.shared.notificationCenter
        nc.addObserver(
            self,
            selector: #selector(handleAppLaunched(_:)),
            name: NSWorkspace.didLaunchApplicationNotification,
            object: nil
        )
        nc.addObserver(
            self,
            selector: #selector(handleAppTerminated(_:)),
            name: NSWorkspace.didTerminateApplicationNotification,
            object: nil
        )
        nc.addObserver(
            self,
            selector: #selector(handleAppActivated(_:)),
            name: NSWorkspace.didActivateApplicationNotification,
            object: nil
        )
    }

    private func isChatGPTRunning() -> Bool {
        NSWorkspace.shared.runningApplications.contains {
            $0.bundleIdentifier == Self.chatGPTBundleID && !$0.isTerminated
        }
    }

    private func isChatGPTOrSelfFrontmost() -> Bool {
        guard let front = NSWorkspace.shared.frontmostApplication else { return false }
        if front.bundleIdentifier == Self.chatGPTBundleID {
            return true
        }
        return front.processIdentifier == ProcessInfo.processInfo.processIdentifier
    }

    private func bootstrapInitialLinkState() {
        if displayMode == .alwaysOnTop {
            backendSupervisor.ensureRunning { [weak self] in
                self?.hudController?.reload()
            }
            evaluateHudVisibility()
            return
        }

        if isChatGPTRunning() {
            backendSupervisor.ensureRunning { [weak self] in
                guard let self = self else { return }
                if self.displayMode != .menuBarOnly || self.hudController?.isVisible == true {
                    self.hudController?.reload()
                }
                if self.displayMode == .projectLinked {
                    self.pollMicroOnce()
                }
            }
            if displayMode == .projectLinked {
                startMicroPolling()
            }
        }
        evaluateHudVisibility()
    }

    @objc private func handleAppLaunched(_ notification: Notification) {
        guard let app = notification.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication,
              app.bundleIdentifier == Self.chatGPTBundleID else {
            return
        }

        backendSupervisor.ensureRunning { [weak self] in
            guard let self = self else { return }
            if self.displayMode != .menuBarOnly || self.hudController?.isVisible == true {
                self.hudController?.reload()
            }
            if self.displayMode == .projectLinked {
                self.pollMicroOnce()
            }
        }

        if displayMode == .projectLinked {
            startMicroPolling()
        }
        evaluateHudVisibility()
    }

    @objc private func handleAppTerminated(_ notification: Notification) {
        guard let app = notification.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication,
              app.bundleIdentifier == Self.chatGPTBundleID else {
            return
        }

        // Wait briefly in case another instance exists; otherwise tear down linked state cleanly
        if !isChatGPTRunning() {
            stopMicroPolling()
            clearDetectedProjectState()
            if displayMode != .alwaysOnTop {
                temporarilyShownInMenuBarOnly = false
                hudController?.hide()
                backendSupervisor.stopOwnedBackend(synchronous: false)
            }
            evaluateHudVisibility()
        }
    }

    @objc private func handleAppActivated(_ notification: Notification) {
        if let app = notification.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication,
           app.bundleIdentifier == Self.chatGPTBundleID {
            backendSupervisor.ensureRunning()
            if displayMode == .projectLinked {
                startMicroPolling()
                pollMicroOnce()
            }
        }
        evaluateHudVisibility()
    }

    // MARK: - Micro Project Polling

    private func startMicroPolling() {
        guard displayMode == .projectLinked, isChatGPTRunning() else {
            stopMicroPolling()
            return
        }
        if microPollTimer != nil { return }
        pollMicroOnce()
        microPollTimer = Timer.scheduledTimer(
            withTimeInterval: Self.microPollInterval,
            repeats: true
        ) { [weak self] _ in
            guard let self = self else { return }
            guard self.displayMode == .projectLinked, self.isChatGPTRunning() else {
                self.stopMicroPolling()
                return
            }
            self.pollMicroOnce()
        }
    }

    private func stopMicroPolling() {
        microPollTimer?.invalidate()
        microPollTimer = nil
    }

    private func clearDetectedProjectState() {
        hasActiveProjectConversation = false
        detectedProjectId = nil
        detectedProjectName = nil
        detectedSessionId = nil
    }

    private func pollMicroOnce() {
        guard !isPollingMicro else { return }
        isPollingMicro = true

        backendSupervisor.fetchMicroSummary { [weak self] result in
            guard let self = self else { return }
            self.isPollingMicro = false

            switch result {
            case .failure:
                if self.isChatGPTRunning() && !self.backendSupervisor.isHealthy {
                    self.backendSupervisor.ensureRunning()
                }
                self.syncMenuState()

            case .success(let summary):
                let primarySession: MicroSessionSummary?
                if let activeId = summary.activeSessionId,
                   let matched = summary.sessions.first(where: { $0.sessionId == activeId }) {
                    primarySession = matched
                } else {
                    primarySession = summary.sessions.first
                }

                // Prefer the active session if it belongs to a project; if active_session_id
                // is a concurrent non-research chat (project_id == nil), fall back to any
                // actively running/blocked/stalled research project session.
                let resolvedProjectSession: MicroSessionSummary?
                if let primary = primarySession, self.isActiveProjectSession(primary) {
                    resolvedProjectSession = primary
                } else if primarySession?.projectId?.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ?? true {
                    resolvedProjectSession = summary.sessions.first { session in
                        self.hasProjectId(session) && self.isLiveActiveStatus(session)
                    }
                } else {
                    resolvedProjectSession = nil
                }

                if let session = resolvedProjectSession {
                    let prevSessionId = self.detectedSessionId
                    self.hasActiveProjectConversation = true
                    self.detectedProjectId = session.projectId
                    self.detectedProjectName = session.projectName ?? session.projectId
                    self.detectedSessionId = session.sessionId

                    // Switching to a new project conversation clears prior manual hide
                    if self.manuallyHidden,
                       let hiddenSid = self.manuallyHiddenForSessionId,
                       hiddenSid != session.sessionId {
                        self.manuallyHidden = false
                        self.manuallyHiddenForSessionId = nil
                    } else if self.manuallyHidden && prevSessionId == nil && self.manuallyHiddenForSessionId == nil {
                        self.manuallyHidden = false
                    }
                } else {
                    self.clearDetectedProjectState()
                }

                self.evaluateHudVisibility()
            }
        }
    }

    private func hasProjectId(_ session: MicroSessionSummary) -> Bool {
        guard let pid = session.projectId?.trimmingCharacters(in: .whitespacesAndNewlines) else {
            return false
        }
        return !pid.isEmpty
    }

    private func isLiveActiveStatus(_ session: MicroSessionSummary) -> Bool {
        guard let status = session.status?.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() else {
            return false
        }
        return Self.activeSessionStatuses.contains(status)
    }

    private func isActiveProjectSession(_ session: MicroSessionSummary) -> Bool {
        guard hasProjectId(session) else {
            return false
        }
        if isLiveActiveStatus(session) {
            return true
        }
        if let updatedRaw = session.updatedAt, let updatedDate = parseISO8601Date(updatedRaw) {
            return Date().timeIntervalSince(updatedDate) <= Self.recentSessionThresholdSeconds
        }
        return false
    }

    private func parseISO8601Date(_ raw: String) -> Date? {
        if let d = isoFormatterFractional.date(from: raw) {
            return d
        }
        return isoFormatterPlain.date(from: raw)
    }

    private func evaluateHudVisibility() {
        guard let hud = hudController else { return }

        let shouldShow: Bool
        if manuallyHidden {
            shouldShow = false
        } else {
            switch displayMode {
            case .menuBarOnly:
                shouldShow = temporarilyShownInMenuBarOnly
            case .alwaysOnTop:
                shouldShow = true
            case .chatgptAlways:
                shouldShow = isChatGPTRunning() && isChatGPTOrSelfFrontmost()
            case .projectLinked:
                shouldShow = isChatGPTRunning() && isChatGPTOrSelfFrontmost() && hasActiveProjectConversation
            }
        }

        if shouldShow {
            if !hud.isVisible {
                hud.show()
            }
        } else {
            if hud.isVisible {
                hud.hide()
            }
        }

        syncMenuState()
    }

    // MARK: - Menu Bar UI

    private func setupStatusBar() {
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let button = item.button {
            if let symbol = NSImage(systemSymbolName: "checklist.checked", accessibilityDescription: "小研进度")
                ?? NSImage(systemSymbolName: "checklist", accessibilityDescription: "小研进度") {
                symbol.isTemplate = true
                button.image = symbol
                button.imagePosition = .imageLeading
                button.title = " 小研进度"
            } else {
                button.title = "小研进度"
            }
            button.toolTip = "小研科研工作流进度浮窗 (127.0.0.1:\(ApprovalLauncher.serverPort))"
        }

        let menu = NSMenu()
        menu.delegate = self
        menu.autoenablesItems = false

        let statusLine = NSMenuItem(title: "联动状态：初始化中…", action: nil, keyEquivalent: "")
        statusLine.isEnabled = false
        menu.addItem(statusLine)
        self.linkStatusMenuItem = statusLine

        menu.addItem(NSMenuItem.separator())

        for mode in HudDisplayMode.allCases {
            let modeItem = NSMenuItem(
                title: mode.menuTitle,
                action: #selector(selectDisplayMode(_:)),
                keyEquivalent: ""
            )
            modeItem.target = self
            modeItem.representedObject = mode.rawValue
            menu.addItem(modeItem)
            modeMenuItems[mode] = modeItem
        }

        menu.addItem(NSMenuItem.separator())

        let toggleVisItem = NSMenuItem(
            title: "显示右上角进度浮窗",
            action: #selector(toggleHudVisibility),
            keyEquivalent: ""
        )
        toggleVisItem.target = self
        menu.addItem(toggleVisItem)
        self.toggleVisibilityMenuItem = toggleVisItem

        let toggleModeItem = NSMenuItem(
            title: "展开进度卡",
            action: #selector(toggleHudMode),
            keyEquivalent: ""
        )
        toggleModeItem.target = self
        menu.addItem(toggleModeItem)
        self.toggleModeMenuItem = toggleModeItem

        let resetPosItem = NSMenuItem(
            title: "重置位置到右上角",
            action: #selector(resetHudPosition),
            keyEquivalent: ""
        )
        resetPosItem.target = self
        menu.addItem(resetPosItem)

        let openDashItem = NSMenuItem(
            title: "打开宏观总览大盘 (http://127.0.0.1:\(ApprovalLauncher.serverPort))",
            action: #selector(openDashboard),
            keyEquivalent: ""
        )
        openDashItem.target = self
        menu.addItem(openDashItem)

        menu.addItem(NSMenuItem.separator())

        let approvalsItem = NSMenuItem(title: "审批与签署 (TTY)", action: nil, keyEquivalent: "")
        let submenu = NSMenu(title: "审批与签署 (TTY)")
        submenu.autoenablesItems = false
        let placeholder = NSMenuItem(title: "正在加载待审批项…", action: nil, keyEquivalent: "")
        placeholder.isEnabled = false
        submenu.addItem(placeholder)
        approvalsItem.submenu = submenu
        menu.addItem(approvalsItem)
        self.approvalsParentItem = approvalsItem
        self.approvalsSubmenu = submenu

        let reloadItem = NSMenuItem(
            title: "刷新浮窗",
            action: #selector(reloadHud),
            keyEquivalent: ""
        )
        reloadItem.target = self
        menu.addItem(reloadItem)

        let restartBackendItem = NSMenuItem(
            title: "重启后端服务",
            action: #selector(restartBackend),
            keyEquivalent: ""
        )
        restartBackendItem.target = self
        menu.addItem(restartBackendItem)

        menu.addItem(NSMenuItem.separator())

        let quitItem = NSMenuItem(
            title: "退出小研进度浮窗",
            action: #selector(quitApp),
            keyEquivalent: "q"
        )
        quitItem.target = self
        menu.addItem(quitItem)

        item.menu = menu
        self.statusItem = item
        self.mainMenu = menu
        syncMenuState()
    }

    private func computeLinkStatusText() -> String {
        let chatRunning = isChatGPTRunning()
        switch displayMode {
        case .menuBarOnly:
            return "模式：仅菜单栏（实时进度已并入 Codex 右侧栏）"

        case .alwaysOnTop:
            if let proj = detectedProjectName, !proj.isEmpty {
                return "联动状态：全局置顶 · \(proj)"
            }
            return backendSupervisor.isHealthy
                ? "联动状态：始终全局置顶（后端运行中）"
                : "联动状态：始终全局置顶（正在启动后端）"

        case .chatgptAlways:
            if !chatRunning {
                return "待机中：等待打开 ChatGPT"
            }
            if let proj = detectedProjectName, !proj.isEmpty {
                return "联动状态：已联动 ChatGPT · \(proj)"
            }
            return "联动状态：已联动 ChatGPT（打开即显示）"

        case .projectLinked:
            if !chatRunning {
                return "待机中：等待打开 ChatGPT"
            }
            if hasActiveProjectConversation, let proj = detectedProjectName, !proj.isEmpty {
                return manuallyHidden
                    ? "联动状态：已手动隐藏 · \(proj)"
                    : "联动状态：已联动 ChatGPT · \(proj)"
            }
            return "已联动 ChatGPT（等待进入课题对话）"
        }
    }

    private func syncMenuState() {
        linkStatusMenuItem?.title = computeLinkStatusText()

        for (mode, item) in modeMenuItems {
            item.state = (mode == displayMode) ? .on : .off
        }

        if let hud = hudController {
            toggleVisibilityMenuItem?.title = hud.isVisible ? "隐藏右上角进度浮窗" : "显示右上角进度浮窗"
            toggleModeMenuItem?.title = hud.mode == .collapsed ? "展开进度卡" : "折叠进度卡"
        }
    }

    // MARK: - NSMenuDelegate

    func menuWillOpen(_ menu: NSMenu) {
        if menu === mainMenu {
            syncMenuState()
            if backendSupervisor.isHealthy {
                refreshApprovalsSubmenu()
            } else {
                setApprovalsLoadingState()
                backendSupervisor.ensureRunning { [weak self] in
                    self?.refreshApprovalsSubmenu()
                }
            }
        }
    }

    private func setApprovalsLoadingState() {
        guard let submenu = approvalsSubmenu else { return }
        submenu.removeAllItems()
        approvalsParentItem?.title = "审批与签署 (TTY) (启动中…)"
        let loadingItem = NSMenuItem(title: "正在连接本地后端并加载待审批项…", action: nil, keyEquivalent: "")
        loadingItem.isEnabled = false
        submenu.addItem(loadingItem)
        submenu.addItem(NSMenuItem.separator())
        let refreshItem = NSMenuItem(
            title: "重新读取审批列表",
            action: #selector(manualRefreshApprovals),
            keyEquivalent: ""
        )
        refreshItem.target = self
        submenu.addItem(refreshItem)
    }

    private func refreshApprovalsSubmenu() {
        guard let submenu = approvalsSubmenu else { return }
        ApprovalLauncher.shared.fetchCommands { [weak self] result in
            guard let self = self else { return }
            submenu.removeAllItems()

            switch result {
            case .failure(let error):
                self.cachedApprovals = []
                self.approvalsParentItem?.title = "审批与签署 (TTY) (待机/离线)"
                let errItem = NSMenuItem(
                    title: "本地后端未运行或暂不可达：\(error.localizedDescription)",
                    action: nil,
                    keyEquivalent: ""
                )
                errItem.isEnabled = false
                submenu.addItem(errItem)

            case .success(let items):
                self.cachedApprovals = items
                let executableCount = items.filter { $0.executable && !$0.governanceConflict }.count
                if items.isEmpty {
                    self.approvalsParentItem?.title = "审批与签署 (TTY) (0)"
                    let empty = NSMenuItem(title: "当前无待审批或待签署项", action: nil, keyEquivalent: "")
                    empty.isEnabled = false
                    submenu.addItem(empty)
                } else {
                    self.approvalsParentItem?.title = "审批与签署 (TTY) (\(executableCount)/\(items.count))"
                    for (idx, item) in items.enumerated() {
                        let shaSuffix = item.sha12.map { " [\($0)]" } ?? ""
                        let prefix: String
                        if item.governanceConflict {
                            prefix = "⚠ [治理冲突] "
                        } else if item.executable {
                            prefix = "▶ "
                        } else {
                            prefix = "⏸ [未就绪] "
                        }

                        let label = "\(prefix)\(item.effectiveProjectLabel) · \(item.targetLabel)\(shaSuffix)"
                        let menuItem = NSMenuItem(
                            title: label,
                            action: #selector(handleApprovalMenuItem(_:)),
                            keyEquivalent: ""
                        )
                        menuItem.target = self
                        menuItem.tag = idx
                        menuItem.isEnabled = item.executable && !item.governanceConflict
                        if item.governanceConflict {
                            menuItem.toolTip = "检测到治理冲突，禁止自动弹出审批终端。"
                        } else if !item.executable {
                            menuItem.toolTip = "当前不可执行（请先完成 preflight 或检查审批状态）。"
                        } else if let cmd = item.displayCmd, !cmd.isEmpty {
                            menuItem.toolTip = cmd
                        }
                        submenu.addItem(menuItem)
                    }
                }
            }

            submenu.addItem(NSMenuItem.separator())
            let refreshItem = NSMenuItem(
                title: "重新读取审批列表",
                action: #selector(manualRefreshApprovals),
                keyEquivalent: ""
            )
            refreshItem.target = self
            submenu.addItem(refreshItem)
        }
    }

    // MARK: - Actions

    @objc private func selectDisplayMode(_ sender: NSMenuItem) {
        guard let raw = sender.representedObject as? String,
              let nextMode = HudDisplayMode(rawValue: raw) else {
            return
        }
        displayMode = nextMode
        UserDefaults.standard.set(nextMode.rawValue, forKey: HudDisplayMode.defaultsKey)
        manuallyHidden = false
        manuallyHiddenForSessionId = nil
        temporarilyShownInMenuBarOnly = false

        switch nextMode {
        case .menuBarOnly:
            stopMicroPolling()
            clearDetectedProjectState()
            if isChatGPTRunning() {
                backendSupervisor.ensureRunning()
            } else {
                backendSupervisor.stopOwnedBackend(synchronous: false)
            }
        case .alwaysOnTop:
            stopMicroPolling()
            backendSupervisor.ensureRunning { [weak self] in
                self?.hudController?.reload()
            }
        case .chatgptAlways:
            stopMicroPolling()
            if isChatGPTRunning() {
                backendSupervisor.ensureRunning { [weak self] in
                    self?.hudController?.reload()
                }
            } else {
                backendSupervisor.stopOwnedBackend(synchronous: false)
            }
        case .projectLinked:
            if isChatGPTRunning() {
                backendSupervisor.ensureRunning { [weak self] in
                    self?.hudController?.reload()
                    self?.pollMicroOnce()
                }
                startMicroPolling()
            } else {
                stopMicroPolling()
                clearDetectedProjectState()
                backendSupervisor.stopOwnedBackend(synchronous: false)
            }
        }

        evaluateHudVisibility()
    }

    @objc private func toggleHudVisibility() {
        guard let hud = hudController else { return }
        if hud.isVisible {
            manuallyHidden = true
            manuallyHiddenForSessionId = detectedSessionId
            temporarilyShownInMenuBarOnly = false
            hud.hide()
        } else {
            manuallyHidden = false
            manuallyHiddenForSessionId = nil
            if displayMode == .menuBarOnly {
                temporarilyShownInMenuBarOnly = true
            }
            backendSupervisor.ensureRunning { [weak self] in
                self?.hudController?.reload()
            }
            hud.show()
        }
        syncMenuState()
    }

    @objc private func toggleHudMode() {
        hudController?.toggleMode()
    }

    @objc private func resetHudPosition() {
        manuallyHidden = false
        manuallyHiddenForSessionId = nil
        if displayMode == .menuBarOnly {
            temporarilyShownInMenuBarOnly = true
        }
        backendSupervisor.ensureRunning { [weak self] in
            self?.hudController?.reload()
        }
        hudController?.resetPositionToTopRight()
        syncMenuState()
    }

    @objc private func openDashboard() {
        backendSupervisor.ensureRunning {
            WebBridge.openDashboardInBrowser(projectId: nil)
        }
    }

    @objc private func reloadHud() {
        backendSupervisor.ensureRunning { [weak self] in
            self?.hudController?.reload()
        }
    }

    @objc private func restartBackend() {
        backendSupervisor.restartBackend { [weak self] in
            guard let self = self else { return }
            if self.displayMode != .menuBarOnly || self.hudController?.isVisible == true {
                self.hudController?.reload()
            }
            if self.displayMode == .projectLinked && self.isChatGPTRunning() {
                self.pollMicroOnce()
            }
        }
    }

    @objc private func manualRefreshApprovals() {
        backendSupervisor.ensureRunning { [weak self] in
            self?.refreshApprovalsSubmenu()
        }
    }

    @objc private func handleApprovalMenuItem(_ sender: NSMenuItem) {
        let index = sender.tag
        guard index >= 0, index < cachedApprovals.count else { return }
        let selected = cachedApprovals[index]
        backendSupervisor.ensureRunning {
            ApprovalLauncher.shared.requestLaunch(approvalId: selected.id, expectedItem: selected)
        }
    }

    @objc private func handleScreenChange() {
        guard hudController?.isVisible == true else { return }
        hudController?.ensureOnScreen()
    }

    @objc private func quitApp() {
        NSApp.terminate(nil)
    }
}
