// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "MeetingRecorder",
    platforms: [.macOS(.v13)],
    products: [
        .executable(name: "MeetingRecorder", targets: ["MeetingRecorder"])
    ],
    targets: [
        .executableTarget(
            name: "MeetingRecorder",
            exclude: ["Resources"],
            swiftSettings: [.swiftLanguageMode(.v5)]
        )
    ]
)
