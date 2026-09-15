export function randomId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }

  const random = () => Math.floor(Math.random() * 0x10000).toString(16).padStart(4, "0");
  return `${Date.now().toString(36)}-${random()}${random()}-${random()}-${random()}-${random()}${random()}${random()}`;
}
