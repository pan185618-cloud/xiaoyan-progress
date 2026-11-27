import Foundation
import AppKit
import WebKit

enum HudMode: String {
    case collapsed
    case expanded

    var size: NSSize {
        switch self {
        case .collapsed:
            return NSSize(width: 290, height: 42)
        case .expanded:
            return NSSize(width: 376, height: 540)
        }
    }
}

protocol WebBridgeDelegate: AnyObject {
    func bridgeDidRequestMode(_ mode: HudMode)
    func bridgeDidRequestHide()
    func bridgeDidRequestApproval(approvalId: String)
    func bridgeDidRequestStartDrag()
}

/// Bridges JavaScript messages from `window.webkit.messageHandlers.xyHud.postMessage(...)`
/// into native macOS HUD actions, and syncs native mode changes back into the page.
final class WebBridge: NSObject, WKScriptMessageHandler {
    static let handlerName = "xyHud"

    weak var delegate: WebBridgeDelegate?

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        guard message.name == Self.handlerName else { return }

        let payload: [String: Any]
        if let dict = message.body as? [String: Any] {
            payload = dict
        } else if let raw = message.body as? String,
                  let data = raw.data(using: .utf8),
                  let dict = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] {
            payload = dict
        } else {
            return
        }

        guard let type = payload["type"] as? String else { return }

        DispatchQueue.main.async { [weak self] in
            self?.handleMessage(type: type, payload: payload)
        }
    }

    private func handleMessage(type: String, payload: [String: Any]) {
        switch type {
        case "setMode":
            guard let modeRaw = payload["mode"] as? String,
                  let mode = HudMode(rawValue: modeRaw) else {
                return
            }
            delegate?.bridgeDidRequestMode(mode)

        case "openDashboard":
            let projectId = payload["projectId"] as? String
            Self.openDashboardInBrowser(projectId: projectId)

        case "copyText":
            if let text = payload["text"] as? String {
                Self.copyToPasteboard(text)
            }

        case "launchApproval":
            if let approvalId = payload["approvalId"] as? String, !approvalId.isEmpty {
                delegate?.bridgeDidRequestApproval(approvalId: approvalId)
            }

        case "hideHud":
            delegate?.bridgeDidRequestHide()

        case "startDrag":
            delegate?.bridgeDidRequestStartDrag()

        default:
            break
        }
    }

    static func syncModeToWeb(_ mode: HudMode, on webView: WKWebView) {
        let js = "window.__xyHudSetMode?.('\(mode.rawValue)');"
        webView.evaluateJavaScript(js, completionHandler: nil)
    }

    static func openDashboardInBrowser(projectId: String? = nil) {
        var components = URLComponents()
        components.scheme = "http"
        components.host = "127.0.0.1"
        components.port = ApprovalLauncher.serverPort
        components.path = "/"

        if let pid = projectId, !pid.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            components.queryItems = [URLQueryItem(name: "project", value: pid)]
        }

        if let url = components.url {
            NSWorkspace.shared.open(url)
        }
    }

    static func copyToPasteboard(_ text: String) {
        let pasteboard = NSPasteboard.general
        pasteboard.clearContents()
        pasteboard.setString(text, forType: .string)
    }
}
