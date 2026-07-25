import type { McpHttpAuth, McpServerCreate } from "@/lib/api";

export type McpTransport = "http" | "stdio";

export interface McpServerDraft {
  name: string;
  transport: McpTransport;
  url: string;
  httpAuth: McpHttpAuth;
  bearerToken: string;
  command: string;
  args: string;
  env: string;
  /** Forward the calling end user's identity token on every tool call. */
  forwardUserIdentity: boolean;
  /** Outbound header name; blank = the documented default. */
  userIdentityHeader: string;
}

export function emptyMcpServerDraft(): McpServerDraft {
  return {
    name: "",
    transport: "http",
    url: "",
    httpAuth: "none",
    bearerToken: "",
    command: "",
    args: "",
    env: "",
    forwardUserIdentity: false,
    userIdentityHeader: "",
  };
}

function parseArgs(raw: string): string[] {
  return raw
    .split(/[\s,]+/)
    .map((value) => value.trim())
    .filter(Boolean);
}

function parseEnv(raw: string): Record<string, string> {
  const env: Record<string, string> = {};
  for (const rawLine of raw.split("\n")) {
    const line = rawLine.trim();
    if (!line) continue;
    const separator = line.indexOf("=");
    if (separator === -1) continue;
    const key = line.slice(0, separator).trim();
    const value = line.slice(separator + 1).trim();
    if (key) env[key] = value;
  }
  return env;
}

export function buildMcpServerCreate(draft: McpServerDraft): McpServerCreate {
  const name = draft.name.trim();
  if (!name) throw new Error("Name required");

  if (draft.transport === "http") {
    const url = draft.url.trim();
    if (!url) throw new Error("URL required");
    if (draft.httpAuth === "header" && !draft.bearerToken.trim()) {
      throw new Error("Bearer token required");
    }

    const server: McpServerCreate = { name, url };
    if (draft.httpAuth !== "none") server.auth = draft.httpAuth;
    if (draft.httpAuth === "header") {
      server.bearer_token = draft.bearerToken;
    }
    // Only send the header override when forwarding is actually on — a name
    // typed and then toggled off must not reach the server, which rejects an
    // orphaned override.
    if (draft.forwardUserIdentity) {
      server.forward_user_identity = true;
      const header = draft.userIdentityHeader.trim();
      if (header) server.user_identity_header = header;
    }
    return server;
  }

  const command = draft.command.trim();
  if (!command) throw new Error("Command required");

  const server: McpServerCreate = { name, command };
  const args = parseArgs(draft.args);
  if (args.length) server.args = args;
  const env = parseEnv(draft.env);
  if (Object.keys(env).length) server.env = env;
  return server;
}
