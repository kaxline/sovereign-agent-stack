#!/usr/bin/env node
/**
 * Regenerate n8n/demo-data/credentials/*.json when secrets change.
 *
 * Usage:
 *   N8N_ENCRYPTION_KEY=... LIGHTRAG_API_KEY=... NEO4J_PASSWORD=... \
 *     node scripts/generate-n8n-credentials.js
 *
 * N8N_CREDENTIALS_OUT sets the output directory (n8n-import uses it to write
 * into the container's /tmp). Without it the files go to n8n/demo-data/credentials.
 */
const crypto = require("crypto");
const fs = require("fs");
const path = require("path");

const encryptionKey = process.env.N8N_ENCRYPTION_KEY;
const lightragApiKey = process.env.LIGHTRAG_API_KEY;
const neo4jPassword = process.env.NEO4J_PASSWORD || "change-me-please";
const neo4jUser = process.env.NEO4J_USERNAME || "neo4j";

const missing = [
  ["N8N_ENCRYPTION_KEY", encryptionKey],
  ["LIGHTRAG_API_KEY", lightragApiKey],
]
  .filter(([, value]) => !value)
  .map(([name]) => name);
if (missing.length > 0) {
  console.error(`Set ${missing.join(" and ")} (NEO4J_PASSWORD is optional).`);
  process.exit(1);
}

function encrypt(data) {
  const salt = crypto.randomBytes(8);
  const password = Buffer.concat([Buffer.from(encryptionKey, "binary"), salt]);
  const hash1 = crypto.createHash("md5").update(password).digest();
  const hash2 = crypto.createHash("md5")
    .update(Buffer.concat([hash1, password]))
    .digest();
  const hash3 = crypto.createHash("md5")
    .update(Buffer.concat([hash2, password]))
    .digest();
  const key = Buffer.concat([hash1, hash2]);
  const iv = hash3;
  const cipher = crypto.createCipheriv("aes-256-cbc", key, iv);
  const encrypted = Buffer.concat([cipher.update(data, "utf8"), cipher.final()]);
  // OpenSSL/CryptoJS envelope: base64 of "Salted__" + salt + ciphertext as one
  // buffer. Encoding the pieces separately misaligns the base64.
  return Buffer.concat([Buffer.from("Salted__"), salt, encrypted]).toString("base64");
}

const outDir =
  process.env.N8N_CREDENTIALS_OUT ||
  path.join(__dirname, "..", "n8n", "demo-data", "credentials");
fs.mkdirSync(outDir, { recursive: true });
const now = new Date().toISOString();

const files = [
  {
    filename: "LrH7tR4gApiKey01.json",
    doc: {
      id: "LrH7tR4gApiKey01",
      name: "LightRAG API",
      type: "httpHeaderAuth",
      data: encrypt(
        JSON.stringify({ name: "X-API-Key", value: lightragApiKey })
      ),
      nodesAccess: [{ nodeType: "n8n-nodes-base.httpRequest", date: now }],
    },
  },
  {
    filename: "N34jBas1cAuth001.json",
    doc: {
      id: "N34jBas1cAuth001",
      name: "Neo4j",
      type: "httpBasicAuth",
      data: encrypt(
        JSON.stringify({ user: neo4jUser, password: neo4jPassword })
      ),
      nodesAccess: [{ nodeType: "n8n-nodes-base.httpRequest", date: now }],
    },
  },
];

for (const { filename, doc } of files) {
  const payload = {
    createdAt: now,
    updatedAt: now,
    id: doc.id,
    name: doc.name,
    data: doc.data,
    type: doc.type,
    nodesAccess: doc.nodesAccess,
  };
  fs.writeFileSync(
    path.join(outDir, filename),
    JSON.stringify(payload, null, 2) + "\n"
  );
  console.log("Wrote", filename);
}
