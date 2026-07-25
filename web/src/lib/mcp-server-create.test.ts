import { describe, expect, it } from "vitest";

import { buildMcpServerCreate, emptyMcpServerDraft } from "./mcp-server-create";

describe("buildMcpServerCreate", () => {
  it("builds an HTTP Bearer request without stdio fields", () => {
    const server = buildMcpServerCreate({
      ...emptyMcpServerDraft(),
      name: " Linear ",
      url: " https://mcp.linear.app/mcp ",
      httpAuth: "header",
      bearerToken: "Bearer secret-token",
      command: "ignored",
      args: "--ignored",
      env: "IGNORED=value",
    });

    expect(server).toEqual({
      name: "Linear",
      url: "https://mcp.linear.app/mcp",
      auth: "header",
      bearer_token: "Bearer secret-token",
    });
  });

  it("builds OAuth and unauthenticated HTTP requests without a token", () => {
    expect(
      buildMcpServerCreate({
        ...emptyMcpServerDraft(),
        name: "oauth",
        url: "https://example.com/mcp",
        httpAuth: "oauth",
      }),
    ).toEqual({
      name: "oauth",
      url: "https://example.com/mcp",
      auth: "oauth",
    });

    expect(
      buildMcpServerCreate({
        ...emptyMcpServerDraft(),
        name: "public",
        url: "https://example.com/mcp",
      }),
    ).toEqual({
      name: "public",
      url: "https://example.com/mcp",
    });
  });

  it("parses stdio arguments and environment assignments", () => {
    const server = buildMcpServerCreate({
      ...emptyMcpServerDraft(),
      name: "local",
      transport: "stdio",
      command: " uvx ",
      args: "mcp-server, --debug",
      env: "API_KEY=secret\nURL=https://example.com?a=b\nINVALID",
    });

    expect(server).toEqual({
      name: "local",
      command: "uvx",
      args: ["mcp-server", "--debug"],
      env: {
        API_KEY: "secret",
        URL: "https://example.com?a=b",
      },
    });
  });

  it("opts an HTTP server into end-user identity forwarding", () => {
    const server = buildMcpServerCreate({
      ...emptyMcpServerDraft(),
      name: "ragnarok",
      url: "http://app:8000/mcp",
      forwardUserIdentity: true,
    });

    expect(server).toEqual({
      name: "ragnarok",
      url: "http://app:8000/mcp",
      forward_user_identity: true,
    });
  });

  it("sends a custom outbound identity header only when one is given", () => {
    expect(
      buildMcpServerCreate({
        ...emptyMcpServerDraft(),
        name: "ragnarok",
        url: "http://app:8000/mcp",
        forwardUserIdentity: true,
        userIdentityHeader: "  X-RAGnarok-Caller  ",
      }),
    ).toEqual({
      name: "ragnarok",
      url: "http://app:8000/mcp",
      forward_user_identity: true,
      user_identity_header: "X-RAGnarok-Caller",
    });

    // Blank means "use the documented default" — don't send an empty override.
    expect(
      buildMcpServerCreate({
        ...emptyMcpServerDraft(),
        name: "ragnarok",
        url: "http://app:8000/mcp",
        forwardUserIdentity: true,
        userIdentityHeader: "   ",
      }),
    ).toEqual({
      name: "ragnarok",
      url: "http://app:8000/mcp",
      forward_user_identity: true,
    });
  });

  it("omits identity forwarding entirely when it is off", () => {
    const server = buildMcpServerCreate({
      ...emptyMcpServerDraft(),
      name: "public",
      url: "https://example.com/mcp",
      // A header typed then toggled off must not leak into the request.
      userIdentityHeader: "X-Should-Not-Appear",
    });

    expect(server).toEqual({
      name: "public",
      url: "https://example.com/mcp",
    });
  });

  it("drops identity forwarding for stdio servers", () => {
    // stdio has no HTTP headers, so the flag is meaningless there.
    const server = buildMcpServerCreate({
      ...emptyMcpServerDraft(),
      name: "local",
      transport: "stdio",
      command: "uvx",
      forwardUserIdentity: true,
      userIdentityHeader: "X-Nope",
    });

    expect(server).toEqual({ name: "local", command: "uvx" });
  });

  it("rejects missing transport fields and Bearer tokens", () => {
    expect(() => buildMcpServerCreate(emptyMcpServerDraft())).toThrow(
      "Name required",
    );
    expect(() =>
      buildMcpServerCreate({
        ...emptyMcpServerDraft(),
        name: "remote",
      }),
    ).toThrow("URL required");
    expect(() =>
      buildMcpServerCreate({
        ...emptyMcpServerDraft(),
        name: "remote",
        url: "https://example.com/mcp",
        httpAuth: "header",
      }),
    ).toThrow("Bearer token required");
    expect(() =>
      buildMcpServerCreate({
        ...emptyMcpServerDraft(),
        name: "local",
        transport: "stdio",
      }),
    ).toThrow("Command required");
  });
});
