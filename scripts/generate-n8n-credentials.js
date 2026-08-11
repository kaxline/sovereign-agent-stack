#!/usr/bin/env node
/**
 * Regenerate n8n/demo-data/credentials/*.json when secrets change.
 *
 * Usage:
 *   N8N_ENCRYPTION_KEY=... LIGHTRAG_API_KEY=... NEO4J_PASSWORD=... \
 *     node scripts/generate-n8n-credentials.js
 */
const crypto = require("crypto");
const fs = require("fs");
const path = require("path");

const encryptionKey = process.env.N8N_ENCRYPTION_KEY;
const lightragApiKey = process.env.LIGHTRAG_API_KEY;
const neo4jPassword = process.env.NEO4J_PASSWORD || "change-me-please";
const neo4jUser = process.env.NEO4J_USERNAME || "neo4j";

if (!encryptionKey || !lightragApiKey) {
  console.error(
    "Set N8N_ENCRYPTION_KEY and LIGHTRAG_API_KEY (and optionally NEO4J_PASSWORD)."
  );
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
  const encrypted =
    cipher.update(data, "utf8", "base64") + cipher.final("base64");
  return "U2FsdGVkX1" + salt.toString("base64") + encrypted;
}

const outDir = path.join(__dirname, "..", "n8n", "demo-data", "credentials");
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
