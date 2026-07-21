import Foundation

struct ChartAccessibilityRow: Identifiable, Hashable, Sendable {
    let id: String
    let series: String
    let label: String
    let value: Double

    var spokenValue: String {
        "\(series), \(label), \(value.formatted(.number))"
    }
}

extension ChartBlock {
    var accessibilityRows: [ChartAccessibilityRow] {
        series.enumerated().flatMap { seriesIndex, series in
            series.points.enumerated().map { pointIndex, point in
                ChartAccessibilityRow(
                    id: "\(seriesIndex)-\(pointIndex)",
                    series: series.name,
                    label: point.label,
                    value: point.value
                )
            }
        }
    }

    var accessibilitySummary: String {
        let values = accessibilityRows.map(\.spokenValue).joined(separator: "; ")
        return [title ?? "Chart", values].filter { !$0.isEmpty }.joined(separator: ". ")
    }
}

extension TableBlock {
    var accessibilityRows: [String] {
        rows.enumerated().map { rowIndex, row in
            let cells = columns.enumerated().map { columnIndex, column in
                let value = columnIndex < row.count ? row[columnIndex] : ""
                return "\(column): \(value)"
            }
            return "Row \(rowIndex + 1), \(cells.joined(separator: ", "))"
        }
    }
}
