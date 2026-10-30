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
  attachment_id?: string;
}): UIMediaAttachment {
  return {
    kind: explicitMediaKind(media) ?? media.kind ?? "file",
    url: media.url,
    name: media.name,
    ...(typeof media.partial === "string" && media.partial ? { partial: media.partial } : {}),
    ...(typeof media.attachment_id === "string" && media.attachment_id
      ? { attachment_id: media.attachment_id }
      : {}),
  };
}

/** One entry of an ``attachment.status`` frame's ``attachments`` array. */
export interface AttachmentStatusCard {
  name?: string;
  error?: string | null;
  partial?: string;
  attachment_id?: string;
}

/**
 * Merge server ``attachment.status`` cards onto the optimistic upload tiles.
 * The gateway parses uploads in upload order, so the merge is positional;
 * the server name is authoritative for the tile label. ``attachment_id`` is
 * copied onto the tile so later frames (the unlock notice) can find it.
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
      ...(typeof status.attachment_id === "string" && status.attachment_id
        ? { attachment_id: status.attachment_id }
        : {}),
    };
    if (
      current === undefined
      || current.kind !== merged.kind
      || current.url !== merged.url
      || current.name !== merged.name
      || current.partial !== merged.partial
      || current.attachment_id !== merged.attachment_id
    ) changed = true;
    next.push(merged);
  }
  return changed ? next : media;
}

/**
 * Merge one server card onto the tile carrying ``attachmentId`` (MIT-1967
 * re-review item 5): the unlock notice arrives during a later turn, so a
 * positional/last-user merge lands its card on the password reply or the
 * wrong tile. Returns ``media`` unchanged when no tile carries the id.
 */
export function mergeAttachmentStatusById(
  media: UIMediaAttachment[] | undefined,
  attachmentId: string,
  status: AttachmentStatusCard,
): UIMediaAttachment[] | undefined {
  const index = (media ?? []).findIndex((tile) => tile.attachment_id === attachmentId);
  if (index < 0) return media;
  const current = (media as UIMediaAttachment[])[index];
  const merged: UIMediaAttachment = {
    ...current,
    ...(typeof status.name === "string" && status.name ? { name: status.name } : {}),
    ...(typeof status.partial === "string" && status.partial ? { partial: status.partial } : {}),
  };
  if (
    current.name === merged.name
    && current.partial === merged.partial
  ) return media;
  const next = [...(media as UIMediaAttachment[])];
  next[index] = merged;
  return next;
}
