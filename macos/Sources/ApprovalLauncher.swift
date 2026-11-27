import Foundation
import AppKit

/// Represents an allowlisted interactive approval/signing command returned by
/// `GET http://127.0.0.1:3460/api/approvals/commands`.
struct ApprovalCommandItem: Decodable, Equatable {
    let id: String
    let kind: String
    let projectId: String?
    let projectName: String?
    let title: String
    let targetLabel: String
    let version: String?
    let sha12: String?
    let preflightPassed: Bool?
    let governanceConflict: Bool
    let executable: Bool
    let argv: [String]
    let displayCmd: String?
    let sourcePath: String?

    enum CodingKeys: String, CodingKey {
        case id
        case kind
        case projectId = "project_id"
        case projectName = "project_name"
        case title
        case targetLabel = "target_label"
        case version
        case sha12
        case preflightPassed = "preflight_passed"
        case governanceConflict = "governance_conflict"
        case executable
        case argv
        case displayCmd = "display_cmd"
        case sourcePath = "source_path"
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(String.self, forKey: .id)
        kind = try container.decodeIfPresent(String.self, forKey: .kind) ?? ""
        projectId = try container.decodeIfPresent(String.self, forKey: .projectId)
        projectName = try container.decodeIfPresent(String.self, forKey: .projectName)
        title = try container.decodeIfPresent(String.self, forKey: .title) ?? id
        targetLabel = try container.decodeIfPresent(String.self, forKey: .targetLabel) ?? title
        version = try container.decodeIfPresent(String.self, forKey: .version)
        sha12 = try container.decodeIfPresent(String.self, forKey: .sha12)
        preflightPassed = try container.decodeIfPresent(Bool.self, forKey: .preflightPassed)
        governanceConflict = try container.decodeIfPresent(Bool.self, forKey: .governanceConflict) ?? false
        executable = try container.decodeIfPresent(Bool.self, forKey: .executable) ?? false
        argv = try container.decodeIfPresent([String].self, forKey: .argv) ?? []
        displayCmd = try container.decodeIfPresent(String.self, forKey: .displayCmd)
        sourcePath = try container.decodeIfPresent(String.self, forKey: .sourcePath)
    }

    var effectiveProjectLabel: String {
        if let name = projectName, !name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return name
        }
        if let pid = projectId, !pid.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return pid
        }
        return "科研工作流全局"
    }
}

private struct ApprovalCommandsEnvelope: Decodable {
    let items: [ApprovalCommandItem]
}

enum ApprovalLauncherError: LocalizedError {
    case invalidEndpoint
    case networkFailure(String)
    case itemNotFound(String)
    case driftDetected(String)
    case governanceConflict(String)
    case notExecutable(String)
    case allowlistViolation(String)
    case processLaunchFailed(String)

    var errorDescription: String? {
        switch self {
        case .invalidEndpoint:
            return "本地审批接口地址无效。"
        case .networkFailure(let detail):
            return "无法从本地服务获取最新审批列表：\(detail)"
        case .itemNotFound(let id):
            return "未在最新审批列表中找到编号为 \(id) 的待审批项，可能已完成或已变更。"
        case .driftDetected(let detail):
            return "检测到审批目标漂移，已中止启动：\(detail)"
        case .governanceConflict(let detail):
            return "存在治理冲突，禁止自动弹出审批终端：\(detail)"
        case .notExecutable(let detail):
            return "该审批项当前不可执行：\(detail)"
        case .allowlistViolation(let detail):
            return "安全白名单校验未通过，已拒绝执行：\(detail)"
        case .processLaunchFailed(let detail):
            return "启动审批终端失败：\(detail)"
        }
    }
}

/// Strictly native-only, allowlisted TTY approval launcher.
/// Never invokes `/bin/sh -c` or arbitrary commands; validates every executable,
/// script, flag, and path against hardcoded constants before launching `Process`.
final class ApprovalLauncher {
    static let shared = ApprovalLauncher()

    static let researchRoot = "/Users/abc/Desktop/本地工作流（小研）/科研工作流"
    static let allowedPython = "/Users/abc/Desktop/本地工作流（小研）/科研工作流/.venv/bin/python"
    static let allowedScript = "/Users/abc/Desktop/本地工作流（小研）/科研工作流/tools/open_approval_terminal.py"
    static let allowedProjectsRoot = "/Users/abc/Desktop/本地工作流（小研）/科研工作流/projects"

    static var serverPort: Int {
        if let raw = ProcessInfo.processInfo.environment["XY_PROGRESS_PORT"],
           let parsed = Int(raw), parsed > 0, parsed < 65536 {
            return parsed
        }
        return 3460
    }

    static var baseURLString: String {
        "http://127.0.0.1:\(serverPort)"
    }

    private let session: URLSession
    private let safeIdRegex: NSRegularExpression

    private init() {
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 5.0
        config.timeoutIntervalForResource = 8.0
        config.requestCachePolicy = .reloadIgnoringLocalCacheData
        self.session = URLSession(configuration: config)
        self.safeIdRegex = try! NSRegularExpression(pattern: "^[A-Za-z0-9._-]{1,128}$", options: [])
    }

    /// Fetches fresh read-only approval command candidates from local backend.
    func fetchCommands(completion: @escaping (Result<[ApprovalCommandItem], Error>) -> Void) {
        guard let url = URL(string: "\(Self.baseURLString)/api/approvals/commands") else {
            DispatchQueue.main.async {
                completion(.failure(ApprovalLauncherError.invalidEndpoint))
            }
            return
        }

        var request = URLRequest(url: url)
        request.httpMethod = "GET"
        request.cachePolicy = .reloadIgnoringLocalCacheData

        session.dataTask(with: request) { data, response, error in
            if let error = error {
                DispatchQueue.main.async {
                    completion(.failure(ApprovalLauncherError.networkFailure(error.localizedDescription)))
                }
                return
            }

            guard let http = response as? HTTPURLResponse else {
                DispatchQueue.main.async {
                    completion(.failure(ApprovalLauncherError.networkFailure("响应格式异常")))
                }
                return
            }

            guard (200...299).contains(http.statusCode), let data = data else {
                DispatchQueue.main.async {
                    completion(.failure(ApprovalLauncherError.networkFailure("HTTP \(http.statusCode)")))
                }
                return
            }

            do {
                let decoded = try JSONDecoder().decode(ApprovalCommandsEnvelope.self, from: data)
                DispatchQueue.main.async {
                    completion(.success(decoded.items))
                }
            } catch {
                DispatchQueue.main.async {
                    completion(.failure(ApprovalLauncherError.networkFailure("JSON 解析失败：\(error.localizedDescription)")))
                }
            }
        }.resume()
    }

    /// Strictly validates an approval item against the hardcoded allowlist.
    /// Returns the exact validated `argv` array (`[python, script, flags...]`) if safe.
    func validate(item: ApprovalCommandItem) -> Result<[String], ApprovalLauncherError> {
        if item.governanceConflict {
            return .failure(.governanceConflict("\(item.effectiveProjectLabel) / \(item.targetLabel)"))
        }
        if !item.executable {
            return .failure(.notExecutable("\(item.effectiveProjectLabel) / \(item.targetLabel)（请先确认 preflight 状态或是否已批准）"))
        }

        let argv = item.argv
        guard argv.count >= 3 else {
            return .failure(.allowlistViolation("命令参数数量不足（至少需要可执行文件、脚本与允许参数）。"))
        }

        // 1. Validate executable path strictly against hardcoded venv python
        guard argv[0] == Self.allowedPython else {
            return .failure(.allowlistViolation("可执行文件不在白名单内：\(argv[0])"))
        }
        guard FileManager.default.isExecutableFile(atPath: Self.allowedPython) else {
            return .failure(.allowlistViolation("白名单 Python 解释器不存在或不可执行：\(Self.allowedPython)"))
        }

        // 2. Validate script path strictly and verify no symlink escape
        guard argv[1] == Self.allowedScript else {
            return .failure(.allowlistViolation("脚本路径不在白名单内：\(argv[1])"))
        }
        let canonicalRoot = URL(fileURLWithPath: Self.researchRoot).resolvingSymlinksInPath().standardized.path
        let canonicalScript = URL(fileURLWithPath: Self.allowedScript).resolvingSymlinksInPath().standardized.path
        let expectedCanonicalScript = canonicalRoot + "/tools/open_approval_terminal.py"
        guard canonicalScript == expectedCanonicalScript,
              FileManager.default.fileExists(atPath: canonicalScript) else {
            return .failure(.allowlistViolation("审批脚本存在符号链接逃逸或文件缺失。"))
        }

        // 3. Validate flags strictly
        let flags = Array(argv.dropFirst(2))
        if flags == ["--zw30-snapshot"] {
            return .success(argv)
        }
        if flags == ["--baseline-snapshots"] {
            return .success(argv)
        }

        // Otherwise only `--sign-contract <path>` (+ optional `--contract-id <safe-id>` / `--preflight-receipt <path>`)
        var index = 0
        var signContractCount = 0
        var sawContractId = false
        var sawPreflightReceipt = false

        while index < flags.count {
            let flag = flags[index]
            switch flag {
            case "--sign-contract":
                guard index + 1 < flags.count else {
                    return .failure(.allowlistViolation("--sign-contract 缺少目标路径参数。"))
                }
                let rawPath = flags[index + 1]
                if case .failure(let err) = validateProjectPath(rawPath, requireRegularFile: false) {
                    return .failure(err)
                }
                signContractCount += 1
                index += 2

            case "--contract-id":
                guard !sawContractId else {
                    return .failure(.allowlistViolation("不允许重复指定 --contract-id。"))
                }
                guard index + 1 < flags.count else {
                    return .failure(.allowlistViolation("--contract-id 缺少参数值。"))
                }
                let cid = flags[index + 1]
                guard isSafeContractId(cid) else {
                    return .failure(.allowlistViolation("非法 --contract-id：\(cid)"))
                }
                sawContractId = true
                index += 2

            case "--preflight-receipt":
                guard !sawPreflightReceipt else {
                    return .failure(.allowlistViolation("不允许重复指定 --preflight-receipt。"))
                }
                guard index + 1 < flags.count else {
                    return .failure(.allowlistViolation("--preflight-receipt 缺少文件路径参数。"))
                }
                let rawReceipt = flags[index + 1]
                if case .failure(let err) = validateProjectPath(rawReceipt, requireRegularFile: true) {
                    return .failure(err)
                }
                sawPreflightReceipt = true
                index += 2

            default:
                return .failure(.allowlistViolation("检测到未在白名单内的参数：\(flag)"))
            }
        }

        guard signContractCount >= 1 else {
            return .failure(.allowlistViolation("缺少允许的主操作参数（--zw30-snapshot / --baseline-snapshots / --sign-contract）。"))
        }
        if (sawContractId || sawPreflightReceipt) && signContractCount != 1 {
            return .failure(.allowlistViolation("--contract-id 与 --preflight-receipt 仅允许配合单个 --sign-contract 使用。"))
        }

        return .success(argv)
    }

    private func isSafeContractId(_ value: String) -> Bool {
        guard !value.isEmpty,
              !value.contains(".."),
              !value.contains("/"),
              !value.contains("\\"),
              !value.contains("\0") else {
            return false
        }
        let range = NSRange(location: 0, length: value.utf16.count)
        return safeIdRegex.firstMatch(in: value, options: [], range: range) != nil
    }

    private func validateProjectPath(_ rawPath: String, requireRegularFile: Bool) -> Result<String, ApprovalLauncherError> {
        guard !rawPath.isEmpty,
              !rawPath.contains("\0"),
              !rawPath.contains("\n"),
              !rawPath.contains("\r") else {
            return .failure(.allowlistViolation("路径包含非法控制字符。"))
        }

        guard rawPath.hasPrefix("/") else {
            return .failure(.allowlistViolation("路径必须为绝对路径：\(rawPath)"))
        }

        let rawComponents = rawPath.split(separator: "/")
        if rawComponents.contains("..") {
            return .failure(.allowlistViolation("路径禁止包含 `..` 跳转：\(rawPath)"))
        }

        let canonicalProjectsRoot = URL(fileURLWithPath: Self.allowedProjectsRoot)
            .resolvingSymlinksInPath()
            .standardized
            .path
        let requiredPrefix = canonicalProjectsRoot.hasSuffix("/")
            ? canonicalProjectsRoot
            : canonicalProjectsRoot + "/"

        let standardizedInput = URL(fileURLWithPath: rawPath).standardized.path
        let allowedRawPrefix = Self.allowedProjectsRoot.hasSuffix("/")
            ? Self.allowedProjectsRoot
            : Self.allowedProjectsRoot + "/"
        guard standardizedInput.hasPrefix(allowedRawPrefix) else {
            return .failure(.allowlistViolation("路径必须位于 RESEARCH_ROOT/projects/ 内：\(rawPath)"))
        }

        let resolvedURL = URL(fileURLWithPath: rawPath).resolvingSymlinksInPath().standardized
        let resolvedPath = resolvedURL.path
        guard resolvedPath.hasPrefix(requiredPrefix) else {
            return .failure(.allowlistViolation("检测到符号链接逃逸出 projects/ 目录：\(rawPath)"))
        }

        // Also check intermediate symlinks along the path
        if hasEscapingSymlink(rawPath: rawPath, requiredPrefix: requiredPrefix) {
            return .failure(.allowlistViolation("路径中存在指向 projects/ 之外的符号链接：\(rawPath)"))
        }

        var isDir: ObjCBool = false
        guard FileManager.default.fileExists(atPath: resolvedPath, isDirectory: &isDir) else {
            return .failure(.allowlistViolation("目标路径不存在：\(rawPath)"))
        }
        if requireRegularFile && isDir.boolValue {
            return .failure(.allowlistViolation("预期为文件但目标是目录：\(rawPath)"))
        }

        return .success(resolvedPath)
    }

    private func hasEscapingSymlink(rawPath: String, requiredPrefix: String) -> Bool {
        let fm = FileManager.default
        let basePrefix = Self.allowedProjectsRoot.hasSuffix("/")
            ? Self.allowedProjectsRoot
            : Self.allowedProjectsRoot + "/"
        guard rawPath.hasPrefix(basePrefix) else { return true }
        let suffix = String(rawPath.dropFirst(basePrefix.count))
        let parts = suffix.split(separator: "/")
        var current = Self.allowedProjectsRoot
        for part in parts {
            current += "/" + String(part)
            if let attrs = try? fm.attributesOfItem(atPath: current),
               let fileType = attrs[.type] as? FileAttributeType,
               fileType == .typeSymbolicLink {
                let targetResolved = URL(fileURLWithPath: current).resolvingSymlinksInPath().standardized.path
                if !targetResolved.hasPrefix(requiredPrefix) {
                    return true
                }
            }
        }
        return false
    }

    /// End-to-end approval trigger flow:
    /// 1. Fetch fresh `/api/approvals/commands` and verify against optional `expectedItem` for drift.
    /// 2. Validate against strict hardcoded allowlist.
    /// 3. Show native `NSAlert` confirmation dialog displaying exact project, target, version, SHA12, and command.
    /// 4. After user clicks "打开审批终端", perform a final drift re-check and launch `Process` directly without shell interpolation.
    func requestLaunch(
        approvalId: String,
        expectedItem: ApprovalCommandItem? = nil,
        completion: ((Bool, String) -> Void)? = nil
    ) {
        fetchCommands { [weak self] result in
            guard let self = self else { return }
            switch result {
            case .failure(let error):
                self.presentErrorAlert(message: error.localizedDescription)
                completion?(false, error.localizedDescription)

            case .success(let items):
                guard let fresh = items.first(where: { $0.id == approvalId }) else {
                    let err = ApprovalLauncherError.itemNotFound(approvalId)
                    self.presentErrorAlert(message: err.localizedDescription)
                    completion?(false, err.localizedDescription)
                    return
                }

                if let expected = expectedItem {
                    if let driftError = self.checkDrift(expected: expected, fresh: fresh) {
                        self.presentErrorAlert(message: driftError.localizedDescription)
                        completion?(false, driftError.localizedDescription)
                        return
                    }
                }

                switch self.validate(item: fresh) {
                case .failure(let err):
                    self.presentErrorAlert(message: err.localizedDescription)
                    completion?(false, err.localizedDescription)

                case .success(let validatedArgv):
                    let confirmed = self.presentConfirmationAlert(item: fresh, argv: validatedArgv)
                    guard confirmed else {
                        completion?(false, "用户已取消")
                        return
                    }

                    // Final drift check right after confirmation before spawning Process
                    self.fetchCommands { finalResult in
                        switch finalResult {
                        case .failure(let err):
                            self.presentErrorAlert(message: err.localizedDescription)
                            completion?(false, err.localizedDescription)

                        case .success(let latestItems):
                            guard let latest = latestItems.first(where: { $0.id == approvalId }) else {
                                let err = ApprovalLauncherError.itemNotFound(approvalId)
                                self.presentErrorAlert(message: err.localizedDescription)
                                completion?(false, err.localizedDescription)
                                return
                            }
                            if let driftError = self.checkDrift(expected: fresh, fresh: latest) {
                                self.presentErrorAlert(message: driftError.localizedDescription)
                                completion?(false, driftError.localizedDescription)
                                return
                            }
                            switch self.validate(item: latest) {
                            case .failure(let err):
                                self.presentErrorAlert(message: err.localizedDescription)
                                completion?(false, err.localizedDescription)
                            case .success(let finalArgv):
                                self.executeValidated(argv: finalArgv, item: latest, completion: completion)
                            }
                        }
                    }
                }
            }
        }
    }

    private func checkDrift(expected: ApprovalCommandItem, fresh: ApprovalCommandItem) -> ApprovalLauncherError? {
        if expected.sha12 != fresh.sha12 {
            return .driftDetected("SHA12 已变化（原：\(expected.sha12 ?? "无") → 现：\(fresh.sha12 ?? "无")）。请刷新后重新核对。")
        }
        if expected.targetLabel != fresh.targetLabel {
            return .driftDetected("目标标签已变化（原：\(expected.targetLabel) → 现：\(fresh.targetLabel)）。请刷新后重新核对。")
        }
        if expected.argv != fresh.argv {
            return .driftDetected("审批命令参数已变化，请刷新后重新核对。")
        }
        return nil
    }

    private func presentConfirmationAlert(item: ApprovalCommandItem, argv: [String]) -> Bool {
        let alert = NSAlert()
        alert.alertStyle = .informational
        alert.messageText = "确认打开小研审批终端 (TTY)"

        let displayCommand = argv.map { part in
            part.contains(" ") ? "\"\(part)\"" : part
        }.joined(separator: " ")

        let lines = [
            "仅原生小研浮窗允许启动交互式审批终端，请在弹出的 Terminal.app 中人工核对并输入姓名签署。",
            "",
            "课题：\(item.effectiveProjectLabel)",
            "目标：\(item.targetLabel)",
            "标题：\(item.title)",
            "版本：\(item.version ?? "-")",
            "SHA12：\(item.sha12 ?? "-")",
            "",
            "即将执行的固定参数命令：",
            displayCommand
        ]
        alert.informativeText = lines.joined(separator: "\n")
        alert.addButton(withTitle: "打开审批终端")
        alert.addButton(withTitle: "取消")

        NSApp.activate(ignoringOtherApps: true)
        let response = alert.runModal()
        return response == .alertFirstButtonReturn
    }

    private func presentErrorAlert(message: String) {
        DispatchQueue.main.async {
            let alert = NSAlert()
            alert.alertStyle = .warning
            alert.messageText = "审批终端启动被拦截或失败"
            alert.informativeText = message
            alert.addButton(withTitle: "知道了")
            NSApp.activate(ignoringOtherApps: true)
            alert.runModal()
        }
    }

    private func executeValidated(
        argv: [String],
        item: ApprovalCommandItem,
        completion: ((Bool, String) -> Void)?
    ) {
        DispatchQueue.global(qos: .userInitiated).async {
            let process = Process()
            process.executableURL = URL(fileURLWithPath: Self.allowedPython)
            // Fixed arguments array: argv[0] is allowedPython, dropFirst() starts with allowedScript
            process.arguments = Array(argv.dropFirst())
            process.currentDirectoryURL = URL(fileURLWithPath: Self.researchRoot)

            let stdoutPipe = Pipe()
            let stderrPipe = Pipe()
            process.standardOutput = stdoutPipe
            process.standardError = stderrPipe

            do {
                try process.run()
                process.waitUntilExit()
                let errData = stderrPipe.fileHandleForReading.readDataToEndOfFile()
                let outData = stdoutPipe.fileHandleForReading.readDataToEndOfFile()
                let stderrText = String(data: errData, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
                let stdoutText = String(data: outData, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""

                if process.terminationStatus == 0 {
                    DispatchQueue.main.async {
                        completion?(true, "已为 \(item.targetLabel) 打开 Terminal.app 审批终端")
                    }
                } else {
                    let detail = !stderrText.isEmpty ? stderrText : (!stdoutText.isEmpty ? stdoutText : "退出码 \(process.terminationStatus)")
                    self.presentErrorAlert(message: "open_approval_terminal.py 返回错误：\n\(detail)")
                    DispatchQueue.main.async {
                        completion?(false, detail)
                    }
                }
            } catch {
                self.presentErrorAlert(message: ApprovalLauncherError.processLaunchFailed(error.localizedDescription).localizedDescription)
                DispatchQueue.main.async {
                    completion?(false, error.localizedDescription)
                }
            }
        }
    }
}
