// Launcher for the mail helper apps. See docs/email.md.
//
// Each app bundle runs one bundled Python helper. Because launchd starts the
// app, macOS attributes the helper's Apple events (and, for the reader's
// store mode, Full Disk Access) to this app instead of to a terminal or a
// shared interpreter. The helper's scripts live inside the signed bundle, so
// editing the repository can't change what a granted app does.
//
// IMAP passwords: the launcher reads every generic password in the Keychain
// under the service named in launcher.json (`<prefix>.mail-imap`, account =
// the config's account id) and writes them to the helper's stdin as one JSON
// object, with MAIL_CREDENTIALS_FD=0 in its environment. The Keychain checks
// this app's signature, so the passwords reach only the helpers you
// installed, and never appear in a file, an argument or the environment.
// launcher.json can name a keychain file (`keychain`, from MAIL_APPS_KEYCHAIN
// at build time) to search instead of your keychain search list; the tests
// use it with a throwaway keychain.

import Foundation
import Security

func keychainPasswords(service: String, keychainPath: String?) -> [String: String] {
    var scope: [String: Any] = [:]
    if let path = keychainPath {
        var keychain: SecKeychain?
        // SecKeychainOpen is deprecated but is the only way to search one
        // keychain file; it doesn't add the file to the search list.
        guard SecKeychainOpen(path, &keychain) == errSecSuccess, let keychain else {
            FileHandle.standardError.write(Data("launcher: can't open the keychain named in launcher.json\n".utf8))
            return [:]
        }
        scope[kSecMatchSearchList as String] = [keychain]
    }
    let list: [String: Any] = scope.merging([
        kSecClass as String: kSecClassGenericPassword,
        kSecAttrService as String: service,
        kSecMatchLimit as String: kSecMatchLimitAll,
        kSecReturnAttributes as String: true,
    ]) { $1 }
    var found: CFTypeRef?
    guard SecItemCopyMatching(list as CFDictionary, &found) == errSecSuccess,
          let items = found as? [[String: Any]] else { return [:] }
    var out: [String: String] = [:]
    for item in items {
        guard let account = item[kSecAttrAccount as String] as? String else { continue }
        let one: [String: Any] = scope.merging([
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecMatchLimit as String: kSecMatchLimitOne,
            kSecReturnData as String: true,
        ]) { $1 }
        var data: CFTypeRef?
        if SecItemCopyMatching(one as CFDictionary, &data) == errSecSuccess,
           let bytes = data as? Data, let password = String(data: bytes, encoding: .utf8) {
            out[account] = password
        } else {
            FileHandle.standardError.write(Data("launcher: can't read the Keychain password for \(account)\n".utf8))
        }
    }
    return out
}

func fail(_ message: String, _ code: Int32) -> Never {
    FileHandle.standardError.write(Data("launcher: \(message)\n".utf8))
    exit(code)
}

guard let resources = Bundle.main.resourceURL else { fail("no Resources folder; rebuild the app", 70) }
guard let data = try? Data(contentsOf: resources.appendingPathComponent("launcher.json")),
      let config = (try? JSONSerialization.jsonObject(with: data)) as? [String: String],
      let python = config["python"], let entry = config["entry"]
else { fail("launcher.json is missing or incomplete; rebuild the app", 70) }

let script = resources.appendingPathComponent("scripts").appendingPathComponent(entry).path
let child = Process()
child.executableURL = URL(fileURLWithPath: python)
// -I: ignore PYTHON* variables, the user site and the current directory.
// -B: write no bytecode into the signed bundle.
child.arguments = ["-I", "-B", script] + CommandLine.arguments.dropFirst()
var environment = ["PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8"]
if let home = ProcessInfo.processInfo.environment["HOME"] { environment["HOME"] = home }
var credentials: Data? = nil
if let service = config["keychain_service"], !service.isEmpty {
    let keychainPath = config["keychain"].flatMap { $0.isEmpty ? nil : $0 }
    let passwords = keychainPasswords(service: service, keychainPath: keychainPath)
    if !passwords.isEmpty, let json = try? JSONSerialization.data(withJSONObject: passwords) {
        credentials = json
        environment["MAIL_CREDENTIALS_FD"] = "0"
    }
}
child.environment = environment
let stdinPipe = Pipe()
if credentials != nil { child.standardInput = stdinPipe }

var sources: [DispatchSourceSignal] = []
for sig in [SIGTERM, SIGINT] {
    signal(sig, SIG_IGN)
    let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    source.setEventHandler { child.terminate() }
    source.resume()
    sources.append(source)
}

do {
    try child.run()
} catch {
    fail("can't start \(python): \(error.localizedDescription)", 71)
}
if let json = credentials {
    // A few hundred bytes: fits in the pipe's buffer whether or not the
    // helper ever reads it.
    stdinPipe.fileHandleForWriting.write(json)
    try? stdinPipe.fileHandleForWriting.close()
    credentials = nil
}
child.waitUntilExit()
exit(child.terminationStatus)
