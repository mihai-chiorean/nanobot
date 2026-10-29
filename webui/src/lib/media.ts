import type { UIMediaAttachment, UIMediaKind } from "@/lib/types";

const IMAGE_EXTENSIONS = new Set([
  ".png",
  ".jpg",
  ".jpeg",
  ".gif",
  ".webp",
  ".bmp",
  ".ico",
  ".svg",
  ".tif",
  ".tiff",
]);

const VIDEO_EXTENSIONS = new Set([
  ".mp4",
  ".webm",
  ".mov",
  ".m4v",
  ".avi",
  ".mkv",
  ".3gp",
]);

function cleanPath(value: string): string {
  return value.split(/[?#]/, 1)[0]?.toLowerCase() ?? "";
}

function extensionOf(value?: string): string {
  if (!value) return "";
  const path = cleanPath(value);
  const dot = path.lastIndexOf(".");
  if (dot < 0) return "";
  return path.slice(dot);
}

function explicitMediaKind(media: { url?: string; name?: string }): UIMediaKind | null {
  const url = media.url ?? "";
  if (url.startsWith("data:image/")) return "image";
  if (url.startsWith("data:video/")) return "video";

  const ext = extensionOf(media.name) || extensionOf(url);
  if (!ext) return null;
  if (IMAGE_EXTENSIONS.has(ext)) return "image";
  if (VIDEO_EXTENSIONS.has(ext)) return "video";
  return "file";
}

export function inferMediaKind(media: { url?: string; name?: string }): UIMediaKind {
  return explicitMediaKind(media) ?? "file";
}

export function toMediaAttachment(media: {
  url?: string;
  name?: string;
  kind?: UIMediaKind;
  partial?: string;
}): UIMediaAttachment {
  return {
    kind: explicitMediaKind(media) ?? media.kind ?? "file",
    url: media.url,
    name: media.name,
    ...(typeof media.partial === "string" && media.partial ? { partial: media.partial } : {}),
  };
}

/** One entry of an ``attachment.status`` frame's ``attachments`` array. */
export interface AttachmentStatusCard {
  name?: string;
  error?: string | null;
  partial?: string;
}

/**
 * Merge server ``attachment.status`` cards onto the optimistic upload tiles.
 * The gateway parses uploads in upload order, so the merge is positional;
 * the server name is authoritative for the tile label.
 */
export function mergeAttachmentStatuses(
  media: UIMediaAttachment[] | undefined,
  statuses: AttachmentStatusCard[] | undefined,
): UIMediaAttachment[] | undefined {
  if (!statuses || statuses.length === 0) return media;
  const base = media ?? [];
  const count = Math.max(base.length, statuses.length);
  let changed = false;
  const next: UIMediaAttachment[] = [];
  for (let index = 0; index < count; index += 1) {
    const status = statuses[index] ?? {};
    const current = base[index];
    const merged: UIMediaAttachment = {
      ...(current ?? { kind: inferMediaKind({ name: status.name }) }),
      ...(typeof status.name === "string" && status.name ? { name: status.name } : {}),
      ...(typeof status.partial === "string" && status.partial ? { partial: status.partial } : {}),
    };
    if (
      current === undefined
      || current.kind !== merged.kind
      || current.url !== merged.url
      || current.name !== merged.name
      || current.partial !== merged.partial
    ) changed = true;
    next.push(merged);
  }
  return changed ? next : media;
}
