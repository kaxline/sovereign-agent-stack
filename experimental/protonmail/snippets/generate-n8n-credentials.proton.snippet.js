 * Optional Proton Bridge IMAP (when compose/protonmail-bridge/credentials.env exists):
 *   PROTON_BRIDGE_IMAP_USER, PROTON_BRIDGE_IMAP_PASSWORD, PROTON_BRIDGE_HOST, etc.
 */
function loadBridgeCredentials() {
  const credsPath = path.join(
    __dirname,
    "..",
    "compose",
    "protonmail-bridge",
    "credentials.env"
  );
  if (!fs.existsSync(credsPath)) {
    return null;
  }
  const vars = {};
  for (const line of fs.readFileSync(credsPath, "utf8").split("\n")) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const eq = trimmed.indexOf("=");
    if (eq === -1) continue;
    vars[trimmed.slice(0, eq)] = trimmed.slice(eq + 1);
  }
  const user = vars.PROTON_BRIDGE_IMAP_USER || process.env.PROTON_BRIDGE_IMAP_USER;
  const password =
    vars.PROTON_BRIDGE_IMAP_PASSWORD || process.env.PROTON_BRIDGE_IMAP_PASSWORD;
  if (!user || !password) {
    return null;
  }
  return {
    user,
    password,
    host: process.env.PROTON_BRIDGE_HOST || "protonmail-bridge",
    port: Number(process.env.PROTON_BRIDGE_IMAP_PORT || 143),
    secure:
      String(process.env.PROTON_BRIDGE_IMAP_SECURE || "false").toLowerCase() ===
      "true",
    allowUnauthorizedCerts:
      String(
        process.env.PROTON_BRIDGE_ALLOW_UNAUTHORIZED_CERTS || "true"
      ).toLowerCase() !== "false",
  };
}
const bridgeCreds = loadBridgeCredentials();
if (bridgeCreds) {
  files.push({
    filename: "Pr0t0nImap01.json",
    doc: {
      id: "Pr0t0nImap01",
      name: "Proton Mail (Bridge)",
      type: "imap",
      data: encrypt(JSON.stringify(bridgeCreds)),
      nodesAccess: [
        { nodeType: "n8n-nodes-base.emailReadImap", date: now },
      ],
    },
  });
} else {
  const protonPath = path.join(outDir, "Pr0t0nImap01.json");
  if (fs.existsSync(protonPath)) {
    fs.unlinkSync(protonPath);
    console.log("Removed Pr0t0nImap01.json (no Bridge credentials configured)");
  }
}
