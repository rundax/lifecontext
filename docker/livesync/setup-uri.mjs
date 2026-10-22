#!/usr/bin/env node
// Make a Self-hosted LiveSync setup URI for one device from the settings of the server's
// headless client, so the device joins the hub with the same encryption and chunk settings.
//
//   node setup-uri.mjs --hub https://NAME.TAILNET.ts.net --device NAME
//                      [--livesync-src DIR] [--settings FILE] [--out-dir DIR]
//
// The URI carries the hub address, the sync user's password and the end-to-end passphrase,
// encrypted with a setup password. The setup password is read from OUT_DIR/setup.password,
// or generated into it the first time. The URI is written to OUT_DIR/NAME.uri. Both files
// are mode 600 in a mode 700 folder, and neither is printed; show the URI as a QR code with
// `qrencode -t ansiutf8 < FILE`. Delete NAME.uri once the device syncs, and setup.password
// to start a new one.
// Defaults: --livesync-src ~/.local/share/livesync-src (the client of docs/sync.md, which
// provides the encoder), --settings ~/.config/livesync/settings.json, --out-dir
// ~/.config/livesync/device-setup.
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const home = os.homedir();
const options = {
    hub: "",
    device: "",
    "livesync-src": path.join(home, ".local/share/livesync-src"),
    settings: path.join(home, ".config/livesync/settings.json"),
    "out-dir": path.join(home, ".config/livesync/device-setup"),
};

function die(message) {
    console.error(`Error: ${message}`);
    process.exit(1);
}

function usage() {
    const lines = fs.readFileSync(fileURLToPath(import.meta.url), "utf8").split("\n").slice(1);
    const header = lines.slice(0, lines.findIndex((line) => !line.startsWith("//")));
    console.log(header.map((line) => line.replace(/^\/\/ ?/, "")).join("\n"));
}

const args = process.argv.slice(2);
while (args.length > 0) {
    const flag = args.shift();
    if (flag === "--help" || flag === "-h") {
        usage();
        process.exit(0);
    }
    const name = flag.replace(/^--/, "");
    if (!flag.startsWith("--") || !Object.hasOwn(options, name)) die(`unknown option ${flag}`);
    if (args.length === 0) die(`${flag} needs a value`);
    options[name] = args.shift();
}

let hub;
try {
    hub = new URL(options.hub);
} catch {
    hub = undefined;
}
if (
    !hub ||
    hub.protocol !== "https:" ||
    hub.username ||
    hub.password ||
    hub.pathname !== "/" ||
    options.hub.includes("?") ||
    options.hub.includes("#")
) {
    die("--hub must be the https:// address from `tailscale serve status`, without a path");
}
if (!/^[A-Za-z0-9-]{1,32}$/.test(options.device)) die("--device must be a short name such as iphone or mac");

// Everything is checked before anything is written. A parse error is reported without its
// text, which would quote the settings file and its secrets.
let server;
try {
    server = JSON.parse(fs.readFileSync(options.settings, "utf8"));
} catch {
    die(`${options.settings} could not be read as JSON`);
}
if (!server || typeof server !== "object" || !server.encrypt || !server.passphrase) {
    die(`${options.settings} has no end-to-end passphrase`);
}
const encoder = path.join(
    options["livesync-src"],
    "node_modules/@vrtmrz/livesync-commonlib/dist/API/processSetting.js"
);
if (!fs.existsSync(encoder)) die(`${encoder} not found; build the client of docs/sync.md first`);
const { encodeSettingsToSetupURI, decodeSettingsFromSetupURI } = await import(pathToFileURL(encoder).href);

process.umask(0o077);
const outDir = options["out-dir"];
fs.mkdirSync(outDir, { recursive: true, mode: 0o700 });
// A folder that already existed is left as it is, but it must not be open to other users.
if (fs.statSync(outDir).mode & 0o077) die(`${outDir} is accessible to other users; run chmod 700 on it`);
const passwordFile = path.join(outDir, "setup.password");
// Letters and digits only, so it can be typed on a phone.
const alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789";
const generated = Array.from({ length: 16 }, () => alphabet[crypto.randomInt(alphabet.length)]).join("");
try {
    fs.writeFileSync(passwordFile, `${generated}\n`, { flag: "wx", mode: 0o600 });
} catch (error) {
    if (error.code !== "EEXIST") throw error;
}
fs.chmodSync(passwordFile, 0o600);
const password = fs.readFileSync(passwordFile, "utf8").trim();
if (!/^[A-Za-z0-9]{12,}$/.test(password)) {
    die(`${passwordFile} does not hold a setup password of 12 or more letters and digits; delete it to make a new one`);
}

const settings = {
    ...server,
    couchDB_URI: hub.origin,
    deviceAndVaultName: options.device,
    // Saved remotes take precedence over couchDB_URI when the plugin loads the settings;
    // the server's would point the device at the server's own loopback address.
    remoteConfigurations: {},
    activeConfigurationId: "",
    P2P_ActiveRemoteConfigurationId: "",
    // The continuous LiveSync mode; the other triggers only apply to the other modes.
    liveSync: true,
    periodicReplication: false,
    syncOnSave: false,
    syncOnEditorSave: false,
    syncOnStart: false,
    syncOnFileOpen: false,
    syncAfterMerge: false,
};
// The same arguments the plugin uses for its own "Copy settings as a new setup URI".
const uri = (await encodeSettingsToSetupURI(settings, password, ["pluginSyncExtendedSetting"], true)).trim();
const decoded = await decodeSettingsFromSetupURI(uri, password);
for (const key of ["couchDB_URI", "couchDB_DBNAME", "couchDB_USER", "couchDB_PASSWORD", "passphrase", "encrypt"]) {
    if (decoded[key] !== settings[key]) die(`the setup URI does not decode back to the same ${key}`);
}

// Written without a trailing newline, so a QR code holds the URI and nothing else.
const uriFile = path.join(outDir, `${options.device}.uri`);
const partial = `${uriFile}.${process.pid}.tmp`;
fs.writeFileSync(partial, uri, { flag: "wx", mode: 0o600 });
fs.renameSync(partial, uriFile);
console.log(`Setup URI for ${options.device}: ${uriFile}`);
console.log(`Setup password: ${passwordFile}`);
console.log(`QR code: qrencode -t ansiutf8 < ${uriFile}`);
