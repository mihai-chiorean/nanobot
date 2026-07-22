import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ThreadComposer } from "@/components/thread/ThreadComposer";

describe("ThreadComposer", () => {
  it("renders a readonly hero model composer when provided", () => {
    render(
      <ThreadComposer
        onSend={vi.fn()}
        modelLabel="claude-opus-4-5"
        placeholder="What's on your mind?"
        variant="hero"
      />,
    );

    expect(screen.getByText("claude-opus-4-5")).toBeInTheDocument();
    const input = screen.getByPlaceholderText("What's on your mind?");
    expect(input).toBeInTheDocument();
    expect(input.className).toContain("min-h-[96px]");
    expect(input.parentElement?.className).toContain("max-w-[40rem]");
  });

  it("sends the composer value to the background handler", () => {
    const onSend = vi.fn();
    const onSendBackground = vi.fn();
    render(
      <ThreadComposer
        onSend={onSend}
        onSendBackground={onSendBackground}
        placeholder="What's on your mind?"
      />,
    );

    fireEvent.change(screen.getByLabelText("Message input"), {
      target: { value: "write a market brief" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Run in background" }));

    expect(onSend).not.toHaveBeenCalled();
    expect(onSendBackground).toHaveBeenCalledWith(
      "write a market brief",
      undefined,
    );
  });
});
