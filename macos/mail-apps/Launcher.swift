// Launcher for the mail helper apps. See docs/email.md.
//
// Each app bundle runs one bundled Python helper. Because launchd starts the
// app, macOS attributes the helper's Apple events (and, for the reader's
// store mode, Full Disk Access) to this app instead of to a terminal or a
// shared interpreter. The helper's scripts live inside the signed bundle, so
// editing the repository can't change what a granted app does.

import Foundation

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
child.environment = environment

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
child.waitUntilExit()
exit(child.terminationStatus)
