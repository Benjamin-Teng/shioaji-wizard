import Darwin
import Foundation

guard let resources = Bundle.main.resourceURL else {
    fputs("找不到 Shioaji 測試精靈的內嵌執行環境。\n", stderr)
    exit(EXIT_FAILURE)
}

let python = resources.appendingPathComponent("python/bin/python3.12").path
let dataRoot = FileManager.default.homeDirectoryForCurrentUser
    .appendingPathComponent("Library/Application Support/ShioajiWizard").path

unsetenv("PYTHONHOME")
unsetenv("PYTHONPATH")
setenv("PYTHONDONTWRITEBYTECODE", "1", 1)
setenv("PYTHONNOUSERSITE", "1", 1)

let userArguments = CommandLine.arguments.dropFirst().filter { !$0.hasPrefix("-psn_") }
let arguments = [python, "-m", "shioaji_wizard", "--root", dataRoot] + userArguments
var argv = arguments.map { strdup($0) } + [nil]
_ = python.withCString { execv($0, &argv) }
perror("ShioajiWizard")
exit(EXIT_FAILURE)
