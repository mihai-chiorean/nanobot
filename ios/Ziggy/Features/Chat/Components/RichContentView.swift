import Charts
import SwiftUI
import UIKit

struct RichContentView: View {
    let blocks: [RichBlock]

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            ForEach(Array(blocks.enumerated()), id: \.offset) { _, block in
                RichBlockView(block: block)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .environment(\.openURL, OpenURLAction { url in
            guard ["http", "https"].contains(url.scheme?.lowercased() ?? "") else {
                return .discarded
            }
            return .systemAction
        })
    }
}

private struct RichBlockView: View {
    let block: RichBlock

    @ViewBuilder
    var body: some View {
        switch block {
        case .markdown(let value):
            ZiggyMarkdownText(markdown: value.text)
        case .code(let value):
            RichCodeBlock(value: value)
        case .table(let value):
            RichTableBlock(value: value)
        case .taskList(let value):
            RichTaskListBlock(value: value)
        case .quote(let value):
            RichQuoteBlock(value: value)
        case .divider:
            Divider().overlay(ZiggyPalette.border)
        case .chart(let value):
            RichChartBlock(value: value)
        case .progress(let value):
            RichTimelineBlock(icon: "clock.arrow.circlepath", title: "Progress", text: value.text)
        case .tool(let value):
            RichTimelineBlock(icon: "wrench.and.screwdriver", title: value.name ?? "Tool", text: value.text)
        case .media(let value):
            RichUnavailableBlock(icon: "photo", title: "Media unavailable", detail: value.alt ?? value.name ?? "Media rendering is not enabled yet.")
        case .mermaid(let value):
            RichUnavailableBlock(icon: "point.3.connected.trianglepath.dotted", title: "Diagram unavailable", detail: value.title ?? "Diagram rendering is not enabled yet.")
        case .file(let value):
            RichUnavailableBlock(icon: "doc", title: "File preview unavailable", detail: value.name)
        case .unsupported(let type, _):
            RichUnavailableBlock(icon: "questionmark.square", title: "Content unavailable", detail: "Unsupported block: \(type)")
        }
    }
}

private struct RichCodeBlock: View {
    let value: CodeBlock
    @State private var didCopy = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 8) {
                Text(value.language?.uppercased() ?? "CODE")
                    .font(.caption2.weight(.semibold).monospaced())
                    .foregroundStyle(ZiggyPalette.mutedForeground)
                Spacer(minLength: 12)
                Button {
                    UIPasteboard.general.string = value.code
                    didCopy = true
                    Task { @MainActor in
                        try? await Task.sleep(for: .seconds(1.5))
                        didCopy = false
                    }
                } label: {
                    Image(systemName: didCopy ? "checkmark" : "doc.on.doc")
                        .frame(width: 28, height: 28)
                }
                .buttonStyle(PWAIconButton(size: 28, foreground: ZiggyPalette.mutedForeground))
                .accessibilityLabel(didCopy ? "Code copied" : "Copy code")
            }
            .padding(.horizontal, 11)
            .padding(.vertical, 5)
            .background(ZiggyPalette.accent.opacity(0.75))

            ScrollView(.horizontal, showsIndicators: true) {
                Text(value.code)
                    .font(.system(.callout, design: .monospaced))
                    .foregroundStyle(ZiggyPalette.foreground)
                    .textSelection(.enabled)
                    .fixedSize(horizontal: true, vertical: false)
                    .padding(12)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .background(ZiggyPalette.secondary.opacity(0.75))
        .clipShape(RoundedRectangle(cornerRadius: 7))
        .overlay(RoundedRectangle(cornerRadius: 7).stroke(ZiggyPalette.border.opacity(0.85)))
    }
}

private struct RichTableBlock: View {
    let value: TableBlock

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            if let caption = value.caption {
                Text(caption)
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(ZiggyPalette.mutedForeground)
            }
            ScrollView(.horizontal, showsIndicators: true) {
                Grid(alignment: .leading, horizontalSpacing: 0, verticalSpacing: 0) {
                    GridRow {
                        ForEach(Array(value.columns.enumerated()), id: \.offset) { _, column in
                            cell(column, isHeader: true, accessibilityLabel: "Column \(column)")
                        }
                    }
                    ForEach(Array(value.rows.enumerated()), id: \.offset) { rowIndex, row in
                        GridRow {
                            ForEach(0..<value.columns.count, id: \.self) { index in
                                let text = index < row.count ? row[index] : ""
                                cell(
                                    text,
                                    isHeader: false,
                                    accessibilityLabel: "Row \(rowIndex + 1), \(value.columns[index]): \(text)"
                                )
                            }
                        }
                    }
                }
            }
            .textSelection(.enabled)
            .accessibilityElement(children: .contain)
        }
    }

    private func cell(_ text: String, isHeader: Bool, accessibilityLabel: String) -> some View {
        Text(text)
            .font(isHeader ? .caption.weight(.semibold) : .caption)
            .foregroundStyle(ZiggyPalette.foreground)
            .frame(minWidth: 108, alignment: .leading)
            .padding(.horizontal, 10)
            .padding(.vertical, 8)
            .background(isHeader ? ZiggyPalette.accent : ZiggyPalette.card)
            .overlay(alignment: .trailing) { Rectangle().fill(ZiggyPalette.border).frame(width: 1) }
            .overlay(alignment: .bottom) { Rectangle().fill(ZiggyPalette.border).frame(height: 1) }
            .accessibilityLabel(accessibilityLabel)
    }
}

private struct RichTaskListBlock: View {
    let value: TaskListBlock

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let title = value.title {
                Text(title).font(.subheadline.weight(.semibold))
            }
            ForEach(value.items) { item in
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Image(systemName: item.isCompleted ? "checkmark.square.fill" : "square")
                        .foregroundStyle(item.isCompleted ? ZiggyPalette.emerald : ZiggyPalette.mutedForeground)
                    Text(item.text)
                        .strikethrough(item.isCompleted)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .font(.body)
            }
        }
        .foregroundStyle(ZiggyPalette.foreground)
    }
}

private struct RichQuoteBlock: View {
    let value: QuoteBlock

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Rectangle().fill(ZiggyPalette.mutedForeground.opacity(0.65)).frame(width: 3)
            VStack(alignment: .leading, spacing: 4) {
                Text(value.text).italic().foregroundStyle(ZiggyPalette.foreground.opacity(0.88))
                if let attribution = value.attribution {
                    Text(attribution).font(.caption).foregroundStyle(ZiggyPalette.mutedForeground)
                }
            }
        }
        .padding(.vertical, 2)
    }
}

private struct RichChartBlock: View {
    let value: ChartBlock

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let title = value.title {
                Text(title).font(.subheadline.weight(.semibold))
            }
            if value.series.isEmpty {
                RichUnavailableBlock(icon: "chart.xyaxis.line", title: "Chart unavailable", detail: "No chart data was provided.")
            } else {
                Chart {
                    ForEach(Array(value.series.enumerated()), id: \.offset) { _, series in
                        ForEach(Array(series.points.enumerated()), id: \.offset) { _, point in
                            switch value.chartType {
                            case .line:
                                LineMark(x: .value("Point", point.label), y: .value("Value", point.value))
                            case .bar:
                                BarMark(x: .value("Point", point.label), y: .value("Value", point.value))
                            case .area:
                                AreaMark(x: .value("Point", point.label), y: .value("Value", point.value))
                            }
                        }
                        .foregroundStyle(by: .value("Series", series.name))
                    }
                }
                .frame(minHeight: 210)
                .chartLegend(position: .bottom, alignment: .leading)
                .accessibilityRepresentation {
                    VStack(alignment: .leading) {
                        Text(value.title ?? "Chart")
                        ForEach(value.accessibilityRows) { row in
                            Text(row.spokenValue)
                        }
                    }
                }
            }
        }
        .foregroundStyle(ZiggyPalette.foreground)
    }

}

private struct RichTimelineBlock: View {
    let icon: String
    let title: String
    let text: String

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: icon).font(.caption2).padding(.top, 3)
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.caption.weight(.semibold))
                Text(text).font(.caption.monospaced())
            }
        }
        .foregroundStyle(ZiggyPalette.mutedForeground)
    }
}

private struct RichUnavailableBlock: View {
    let icon: String
    let title: String
    let detail: String

    var body: some View {
        HStack(alignment: .top, spacing: 9) {
            Image(systemName: icon).font(.body).foregroundStyle(ZiggyPalette.mutedForeground)
            VStack(alignment: .leading, spacing: 3) {
                Text(title).font(.subheadline.weight(.semibold))
                Text(detail).font(.caption).foregroundStyle(ZiggyPalette.mutedForeground)
            }
        }
        .foregroundStyle(ZiggyPalette.foreground)
        .padding(11)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(ZiggyPalette.secondary.opacity(0.65))
        .clipShape(RoundedRectangle(cornerRadius: 7))
        .overlay(RoundedRectangle(cornerRadius: 7).stroke(ZiggyPalette.border.opacity(0.8)))
    }
}
