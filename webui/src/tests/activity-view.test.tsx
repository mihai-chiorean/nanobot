import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ActivityView } from "@/components/activity/ActivityView";
import type { WorkTask } from "@/lib/types";
import { ClientProvider } from "@/providers/ClientProvider";
import { useWork } from "@/hooks/useWork";

vi.mock("@/hooks/useWork", () => ({
  useWork: vi.fn(),
}));

const useWorkMock = vi.mocked(useWork);

const task: WorkTask = {
  task_id: "task-1",
  session_key: "websocket:chat-1",
  chat_id: "chat-1",
  title: "Report",
  prompt_preview: "Write a report",
  status: "succeeded",
  mode: "background",
  model: "model",
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:01:00Z",
  last_seq: 2,
  artifact_count: 1,
  artifacts: [
    {
      artifact_id: "artifact-1",
      task_id: "task-1",
      kind: "file",
      name: "report.pdf",
      mime: "application/pdf",
      size_bytes: 4,
      sha256: "hash",
      created_at: "2026-01-01T00:01:00Z",
      url: "/api/work/artifacts/artifact-1",
    },
  ],
};

const client = {
  onWork: vi.fn(() => () => {}),
} as never;

function renderView() {
  return render(
    <ClientProvider client={client} token="rest-token">
      <ActivityView onBackToChat={vi.fn()} onOpenChat={vi.fn()} />
    </ClientProvider>,
  );
}

beforeEach(() => {
  vi.resetAllMocks();
  useWorkMock.mockReturnValue({
    tasks: [task],
    loading: false,
    error: null,
    refresh: vi.fn().mockResolvedValue(undefined),
    loadTask: vi.fn().mockResolvedValue(task),
  });
  vi.stubGlobal("fetch", vi.fn());
});

describe("ActivityView artifact downloads", () => {
  it("downloads an artifact with authentication and revokes its object URL", async () => {
    const blob = new Blob(["data"], { type: "application/pdf" });
    vi.mocked(fetch).mockResolvedValue({
      ok: true,
      blob: async () => blob,
    } as Response);
    const createObjectURL = vi.fn(() => "blob:report");
    const revokeObjectURL = vi.fn();
    vi.stubGlobal("URL", { createObjectURL, revokeObjectURL });

    renderView();
    const download = await screen.findByRole("button", { name: "Download report.pdf" });
    fireEvent.click(download);

    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      "/api/work/artifacts/artifact-1",
      expect.objectContaining({
        headers: { Authorization: "Bearer rest-token" },
        credentials: "same-origin",
      }),
    ));
    await waitFor(() => expect(revokeObjectURL).toHaveBeenCalledWith("blob:report"));
    expect(createObjectURL).toHaveBeenCalledWith(blob);
  });

  it("shows a loading state and a usable error when the artifact request fails", async () => {
    let rejectDownload: (reason: Error) => void = () => {};
    vi.mocked(fetch).mockReturnValue(new Promise((_resolve, reject) => {
      rejectDownload = reject;
    }) as Promise<Response>);

    renderView();
    const download = await screen.findByRole("button", { name: "Download report.pdf" });
    fireEvent.click(download);
    expect(await screen.findByRole("button", { name: "Downloading report.pdf" })).toBeDisabled();

    rejectDownload(new Error("network down"));
    expect(await screen.findByRole("alert")).toHaveTextContent("Download failed");
    expect(screen.getByRole("button", { name: "Download report.pdf" })).toBeEnabled();
  });
});
