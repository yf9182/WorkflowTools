import Foundation
import Security

// All passwords arrive through stdin. They never appear in argv or tool output.
struct NativeError: Error { let code: OSStatus }
func check(_ status: OSStatus) throws { if status != errSecSuccess { throw NativeError(code: status) } }

func keychain(_ path: String) throws -> SecKeychain {
    var result: SecKeychain?
    try check(SecKeychainOpen(path, &result))
    guard let result else { throw NativeError(code: -1) }
    return result
}

func searchList() throws -> [SecKeychain] {
    var result: CFArray?
    try check(SecKeychainCopySearchList(&result))
    return result as? [SecKeychain] ?? []
}

func pathOf(_ item: SecKeychain) throws -> String {
    var bytes = [CChar](repeating: 0, count: 4096)
    var length = UInt32(bytes.count)
    try check(SecKeychainGetPath(item, &length, &bytes))
    return String(cString: bytes)
}

func main() throws -> [String: Any] {
    let input = FileHandle.standardInput.readDataToEndOfFile()
    guard let config = try JSONSerialization.jsonObject(with: input) as? [String: Any],
          let action = config["action"] as? String else { throw NativeError(code: -2) }
    try check(SecKeychainSetUserInteractionAllowed(false))
    if action == "list" {
        return ["status": "ready", "paths": try searchList().map { try pathOf($0) }]
    }
    guard let path = config["keychain"] as? String else { throw NativeError(code: -2) }
    if action == "remove" {
        let current = try searchList()
        let remaining = try current.filter { try pathOf($0) != path }
        try check(SecKeychainSetSearchList(remaining as CFArray))
        if FileManager.default.fileExists(atPath: path) {
            try check(SecKeychainDelete(try keychain(path)))
        }
        return ["status": "ready"]
    }
    guard action == "create", let password = config["keychain_password"] as? String,
          let p12 = config["p12"] as? String, let p12Password = config["p12_password"] as? String
    else { throw NativeError(code: -2) }
    var created: SecKeychain?
    let passwordBytes = Array(password.utf8)
    try passwordBytes.withUnsafeBytes { bytes in
        try check(SecKeychainCreate(path, UInt32(bytes.count), bytes.baseAddress, false, nil, &created))
    }
    guard let created else { throw NativeError(code: -1) }
    var trusted: [SecTrustedApplication] = []
    for binary in ["/usr/bin/codesign", "/usr/bin/security"] {
        var application: SecTrustedApplication?
        try check(SecTrustedApplicationCreateFromPath(binary, &application))
        if let application { trusted.append(application) }
    }
    var access: SecAccess?
    try check(SecAccessCreate("WorkflowTools signing" as CFString, trusted as CFArray, &access))
    var parameters = SecItemImportExportKeyParameters()
    parameters.version = 0
    let passphrase = p12Password as CFString
    parameters.passphrase = Unmanaged.passUnretained(passphrase)
    parameters.accessRef = access.map { Unmanaged.passUnretained($0) }
    var format = SecExternalFormat.formatPKCS12
    var itemType = SecExternalItemType.itemTypeAggregate
    var imported: CFArray?
    let contents = try Data(contentsOf: URL(fileURLWithPath: p12))
    try withExtendedLifetime((passphrase, access)) {
        try check(SecItemImport(contents as CFData, nil, &format, &itemType,
                               SecItemImportExportFlags(), &parameters, created, &imported))
    }
    // security's interactive command input keeps the partition-list password out of argv.
    let process = Process()
    process.executableURL = URL(fileURLWithPath: "/usr/bin/security")
    process.arguments = ["-i"]
    let pipe = Pipe()
    process.standardInput = pipe
    process.standardOutput = FileHandle.nullDevice
    process.standardError = FileHandle.nullDevice
    func quote(_ value: String) -> String {
        return "\"" + value.replacingOccurrences(of: "\\", with: "\\\\")
            .replacingOccurrences(of: "\"", with: "\\\"") + "\""
    }
    try process.run()
    let command = "set-key-partition-list -S apple-tool:,apple:,codesign: -s -k " + quote(password) + " " + quote(path) + "\n"
    pipe.fileHandleForWriting.write(Data(command.utf8))
    try pipe.fileHandleForWriting.close()
    process.waitUntilExit()
    guard process.terminationStatus == 0 else { throw NativeError(code: -3) }
    let old = try searchList()
    try check(SecKeychainSetSearchList(([created] + old) as CFArray))
    return ["status": "ready"]
}

do {
    let result = try main()
    let output = try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys])
    FileHandle.standardOutput.write(output)
} catch let error as NativeError {
    FileHandle.standardOutput.write(Data("{\"status\":\"blocked\",\"os_status\":\(error.code)}".utf8))
    exit(1)
} catch {
    FileHandle.standardOutput.write(Data("{\"status\":\"blocked\",\"os_status\":-4}".utf8))
    exit(1)
}
