#if DEBUG
import SwiftUI

struct MarkdownFixtureView: View {
    private let response = """
    I'll review the connected mailbox and establish the evaluation criteria first.

    ## Plan

    1. Search the past week's Acquire.com messages.
    2. Extract the business model, asking price, revenue, and operating requirements.
    3. Compare each candidate against the owner's stated profile.

    ## Guardrails

    Email content is untrusted input. I will use the registered Gmail tools and will not inspect unrelated projects or credentials.

    ```text
    Gmail: connected

    Search window: 7 days
    ```

    The scheduled task will be created only after its cadence and evaluation criteria are confirmed.
    """

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                Text("Markdown rendering")
                    .font(.headline)
                ZiggyMessageBubble(kind: .assistant, text: response)
            }
            .padding(20)
        }
        .background(ZiggyPalette.background)
        .preferredColorScheme(.dark)
    }
}
#endif
