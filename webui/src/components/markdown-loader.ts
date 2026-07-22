export const loadMarkdownRenderer = () =>
  import("@/components/MarkdownTextRenderer");

export function preloadMarkdownText(): void {
  void loadMarkdownRenderer();
}
