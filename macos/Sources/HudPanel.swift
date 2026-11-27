import Foundation
import AppKit
import SwiftUI
@preconcurrency import WebKit
import QuartzCore

/// Non-activating borderless floating NSPanel that stays above regular windows
/// across Spaces and full-screen apps without stealing focus from ChatGPT Work.
final class FloatingHudPanel: NSPanel {
    init(contentRect: NSRect) {
        super.init(
            contentRect: contentRect,
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        isFloatingPanel = true
        becomesKeyOnlyIfNeeded = true
        hidesOnDeactivate = false
        level = .floating
        collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .ignoresCycle]
        isOpaque = false
        backgroundColor = .clear
        hasShadow = true
        isMovableByWindowBackground = true
        animationBehavior = .none
    }

    override var canBecomeKey: Bool {
        true
    }

    override var canBecomeMain: Bool {
        false
    }
}

/// Transparent top drag strip so the user can always drag the borderless panel
/// smoothly even when WKWebView occupies the full frame.
private final class HudDragStripView: NSView {
    override var mouseDownCanMoveWindow: Bool { true }

    override func mouseDown(with event: NSEvent) {
        window?.performDrag(with: event)
    }
}

/// SwiftUI fallback overlay displayed while `http://127.0.0.1:3460/hud` is booting
/// or temporarily unreachable.
private struct HudFallbackView: View {
    let mode: HudMode
    let statusMessage: String
    let onReload: () -> Void
    let onToggleMode: () -> Void

    var body: some View {
        Group {
            if mode == .collapsed {
                HStack(spacing: 8) {
                    ProgressView()
                        .controlSize(.small)
                    Text("小研进度连接中…")
                        .font(.system(size: 12, weight: .medium))
                        .lineLimit(1)
                    Spacer(minLength: 4)
                    Button("重试", action: onReload)
                        .buttonStyle(.borderless)
                        .font(.system(size: 11, weight: .semibold))
                        .foregroundColor(.accentColor)
                }
                .padding(.horizontal, 12)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 21, style: .continuous))
                .overlay(
                    RoundedRectangle(cornerRadius: 21, style: .continuous)
                        .strokeBorder(Color.primary.opacity(0.12), lineWidth: 1)
                )
            } else {
                VStack(alignment: .leading, spacing: 14) {
                    HStack {
                        Image(systemName: "checklist.checked")
                            .foregroundColor(.accentColor)
                        Text("小研科研进度浮窗")
                            .font(.system(size: 14, weight: .semibold))
                        Spacer()
                        Button(action: onToggleMode) {
                            Image(systemName: "chevron.up.circle")
                        }
                        .buttonStyle(.borderless)
                    }

                    Divider()

                    VStack(alignment: .center, spacing: 10) {
                        ProgressView()
                            .controlSize(.regular)
                        Text("正在等待本地进度服务就绪")
                            .font(.system(size: 13, weight: .medium))
                        Text(statusMessage)
                            .font(.system(size: 11))
                            .foregroundColor(.secondary)
                            .multilineTextAlignment(.center)
                            .lineLimit(4)
                        Button("立即重新加载", action: onReload)
                            .buttonStyle(.borderedProminent)
                            .controlSize(.small)
                    }
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                }
                .padding(16)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
                .overlay(
                    RoundedRectangle(cornerRadius: 16, style: .continuous)
                        .strokeBorder(Color.primary.opacity(0.12), lineWidth: 1)
                )
            }
        }
    }
}

/// Manages the FloatingHudPanel, its transparent WKWebView, top-right anchoring,
/// animated resizing, fallback retry overlay, and JS bridge wiring.
final class HudPanelController: NSObject, WKNavigationDelegate, WKUIDelegate, WebBridgeDelegate {
    private static let edgeMargin: CGFloat = 12.0

    let panel: FloatingHudPanel
    let webView: WKWebView
    let bridge: WebBridge

    private let rootView: NSView
    private var fallbackHostingView: NSHostingView<HudFallbackView>?
    private var retryTimer: Timer?
    private var retryAttempts: Int = 0
    private var lastErrorMessage: String = "http://127.0.0.1:\(ApprovalLauncher.serverPort)/hud"

    private(set) var mode: HudMode = .collapsed {
        didSet {
            onStateChange?()
        }
    }

    var isVisible: Bool {
        panel.isVisible
    }

    var onStateChange: (() -> Void)?
    var onUserRequestedHide: (() -> Void)?

    override init() {
        let initialFrame = Self.topRightFrame(for: .collapsed)
        self.panel = FloatingHudPanel(contentRect: initialFrame)
        self.bridge = WebBridge()

        let contentController = WKUserContentController()
        let config = WKWebViewConfiguration()
        config.userContentController = contentController
        config.websiteDataStore = .nonPersistent()

        let webView = WKWebView(frame: NSRect(origin: .zero, size: initialFrame.size), configuration: config)
        webView.autoresizingMask = [.width, .height]
        webView.setValue(false, forKey: "drawsBackground")
        webView.allowsMagnification = false
        self.webView = webView

        let rootView = NSView(frame: NSRect(origin: .zero, size: initialFrame.size))
        rootView.wantsLayer = true
        rootView.layer?.backgroundColor = NSColor.clear.cgColor
        rootView.autoresizingMask = [.width, .height]
        self.rootView = rootView

        super.init()

        self.bridge.delegate = self
        contentController.add(self.bridge, name: WebBridge.handlerName)
        self.webView.navigationDelegate = self
        self.webView.uiDelegate = self

        rootView.addSubview(webView)

        // Subtle 7pt drag strip at very top edge so panel can always be repositioned effortlessly
        let dragStrip = HudDragStripView(frame: NSRect(x: 16, y: initialFrame.height - 7, width: initialFrame.width - 32, height: 7))
        dragStrip.autoresizingMask = [.width, .minYMargin]
        rootView.addSubview(dragStrip)

        panel.contentView = rootView
        showFallback(message: "正在连接 \(Self.hudURLString()) …")
    }

    deinit {
        retryTimer?.invalidate()
        webView.configuration.userContentController.removeScriptMessageHandler(forName: WebBridge.handlerName)
    }

    static func hudURLString() -> String {
        "\(ApprovalLauncher.baseURLString)/hud"
    }

    static func topRightFrame(for mode: HudMode, on screen: NSScreen? = nil) -> NSRect {
        let targetScreen = screen ?? NSScreen.main ?? NSScreen.screens.first
        let visible = targetScreen?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
        let size = mode.size
        let x = visible.maxX - edgeMargin - size.width
        let y = visible.maxY - edgeMargin - size.height
        return NSRect(x: x, y: y, width: size.width, height: size.height)
    }

    func show() {
        let wasHidden = !panel.isVisible
        panel.orderFrontRegardless()
        if wasHidden && (webView.url == nil || !(fallbackHostingView?.isHidden ?? true)) {
            reload()
        }
        onStateChange?()
    }

    func hide() {
        retryTimer?.invalidate()
        retryTimer = nil
        panel.orderOut(nil)
        onStateChange?()
    }

    func toggleVisibility() {
        if panel.isVisible {
            hide()
        } else {
            show()
        }
    }

    func toggleMode() {
        setMode(mode == .collapsed ? .expanded : .collapsed, syncToWeb: true)
    }

    func setMode(_ newMode: HudMode, syncToWeb: Bool, animated: Bool = true) {
        let oldMode = mode
        mode = newMode

        let targetSize = newMode.size
        let currentFrame = panel.frame

        // Pin the top-right corner during resize
        let topRightX = currentFrame.maxX
        let topRightY = currentFrame.maxY
        var newX = topRightX - targetSize.width
        var newY = topRightY - targetSize.height

        // Keep frame gently inside the active screen's visibleFrame if needed
        if let visible = (panel.screen ?? NSScreen.main ?? NSScreen.screens.first)?.visibleFrame {
            let minX = visible.minX + Self.edgeMargin
            let maxX = visible.maxX - Self.edgeMargin - targetSize.width
            let minY = visible.minY + Self.edgeMargin
            let maxY = visible.maxY - Self.edgeMargin - targetSize.height
            newX = min(max(newX, minX), max(minX, maxX))
            newY = min(max(newY, minY), max(minY, maxY))
        }

        let targetFrame = NSRect(x: newX, y: newY, width: targetSize.width, height: targetSize.height)

        if oldMode != newMode || currentFrame.size != targetSize {
            if animated {
                NSAnimationContext.runAnimationGroup { context in
                    context.duration = 0.22
                    context.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
                    panel.animator().setFrame(targetFrame, display: true)
                }
            } else {
                panel.setFrame(targetFrame, display: true, animate: false)
            }
        }

        refreshFallbackIfVisible()

        if syncToWeb {
            WebBridge.syncModeToWeb(newMode, on: webView)
        }
    }

    func resetPositionToTopRight() {
        let targetFrame = Self.topRightFrame(for: mode, on: NSScreen.main)
        NSAnimationContext.runAnimationGroup { context in
            context.duration = 0.20
            context.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
            panel.animator().setFrame(targetFrame, display: true)
        }
        if !panel.isVisible {
            show()
        }
    }

    func ensureOnScreen() {
        guard let visible = (panel.screen ?? NSScreen.main ?? NSScreen.screens.first)?.visibleFrame else { return }
        if !visible.intersects(panel.frame) {
            resetPositionToTopRight()
        }
    }

    func reload() {
        retryTimer?.invalidate()
        retryTimer = nil
        retryAttempts = 0
        loadHudPage()
    }

    private func loadHudPage() {
        guard let url = URL(string: Self.hudURLString()) else { return }
        var request = URLRequest(url: url)
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.timeoutInterval = 8.0
        webView.load(request)
    }

    private func showFallback(message: String) {
        lastErrorMessage = message
        let view = HudFallbackView(
            mode: mode,
            statusMessage: message,
            onReload: { [weak self] in self?.reload() },
            onToggleMode: { [weak self] in self?.toggleMode() }
        )

        if let host = fallbackHostingView {
            host.rootView = view
            host.isHidden = false
        } else {
            let host = NSHostingView(rootView: view)
            host.frame = rootView.bounds
            host.autoresizingMask = [.width, .height]
            rootView.addSubview(host)
            fallbackHostingView = host
        }
        webView.isHidden = true
    }

    private func refreshFallbackIfVisible() {
        guard let host = fallbackHostingView, !host.isHidden else { return }
        host.rootView = HudFallbackView(
            mode: mode,
            statusMessage: lastErrorMessage,
            onReload: { [weak self] in self?.reload() },
            onToggleMode: { [weak self] in self?.toggleMode() }
        )
    }

    private func hideFallback() {
        fallbackHostingView?.isHidden = true
        webView.isHidden = false
    }

    private func scheduleRetry(reason: String) {
        retryTimer?.invalidate()
        guard panel.isVisible else {
            showFallback(message: reason)
            return
        }
        retryAttempts += 1
        let delay: TimeInterval = retryAttempts <= 5 ? 2.0 : (retryAttempts <= 15 ? 3.5 : 5.0)
        showFallback(message: "\(reason)\n将在 \(Int(delay)) 秒后自动重试（第 \(retryAttempts) 次）")
        retryTimer = Timer.scheduledTimer(withTimeInterval: delay, repeats: false) { [weak self] _ in
            guard let self = self, self.panel.isVisible else { return }
            self.loadHudPage()
        }
    }

    // MARK: - WKNavigationDelegate

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        retryTimer?.invalidate()
        retryTimer = nil
        retryAttempts = 0
        hideFallback()
        WebBridge.syncModeToWeb(mode, on: webView)
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        let nsError = error as NSError
        if nsError.domain == NSURLErrorDomain && nsError.code == NSURLErrorCancelled {
            return
        }
        scheduleRetry(reason: "本地后端暂未就绪：\(error.localizedDescription)")
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        let nsError = error as NSError
        if nsError.domain == NSURLErrorDomain && nsError.code == NSURLErrorCancelled {
            return
        }
        scheduleRetry(reason: "页面加载中断：\(error.localizedDescription)")
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        scheduleRetry(reason: "渲染进程已重置，正在恢复…")
    }

    func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationAction: WKNavigationAction,
        decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
    ) {
        guard let url = navigationAction.request.url else {
            decisionHandler(.cancel)
            return
        }
        let isLocalHost = (url.host == "127.0.0.1" || url.host == "localhost")
        if isLocalHost && url.path.hasPrefix("/hud") {
            decisionHandler(.allow)
            return
        }
        if navigationAction.navigationType == .linkActivated {
            NSWorkspace.shared.open(url)
            decisionHandler(.cancel)
            return
        }
        decisionHandler(isLocalHost ? .allow : .cancel)
    }

    // MARK: - WKUIDelegate

    func webView(
        _ webView: WKWebView,
        createWebViewWith configuration: WKWebViewConfiguration,
        for navigationAction: WKNavigationAction,
        windowFeatures: WKWindowFeatures
    ) -> WKWebView? {
        if let url = navigationAction.request.url,
           url.scheme == "http" || url.scheme == "https" {
            NSWorkspace.shared.open(url)
        }
        return nil
    }

    // MARK: - WebBridgeDelegate

    func bridgeDidRequestMode(_ mode: HudMode) {
        setMode(mode, syncToWeb: false, animated: true)
    }

    func bridgeDidRequestHide() {
        hide()
        onUserRequestedHide?()
    }

    func bridgeDidRequestApproval(approvalId: String) {
        ApprovalLauncher.shared.requestLaunch(approvalId: approvalId)
    }

    func bridgeDidRequestStartDrag() {
        if let event = NSApp.currentEvent {
            panel.performDrag(with: event)
        }
    }
}
