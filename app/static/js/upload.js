// Upload modal: choose a file, set its label, add it to the brain.

import { uploadDocument } from "./api.js";
import { qs, toast } from "./dom.js";
import { getWorkspaceScope } from "./state.js";

// The single source of truth for what onebrain can extract. Used to build the
// native picker's filter AND to reject unsupported drops/picks in the UI.
const SUPPORTED_EXTS = [
  ".pdf", ".docx", ".xlsx", ".xlsm", ".pptx", ".rtf",
  ".txt", ".md", ".markdown", ".csv", ".tsv", ".json",
  ".html", ".htm", ".xml", ".yaml", ".yml", ".log",
  ".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp", ".gif", ".webp",
];

// extension -> a short badge label + colour class, for the selected-file card.
const BADGES = [
  [[".pdf"], "PDF", "pdf"],
  [[".docx"], "DOC", "doc"],
  [[".xlsx", ".xlsm", ".csv", ".tsv"], "XLS", "xls"],
  [[".pptx"], "PPT", "ppt"],
  [[".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp", ".gif", ".webp"], "IMG", "img"],
];

const extOf = (name) => {
  const i = name.lastIndexOf(".");
  return i < 0 ? "" : name.slice(i).toLowerCase();
};
const isSupported = (name) => SUPPORTED_EXTS.includes(extOf(name));

function badgeFor(name) {
  const ext = extOf(name);
  for (const [exts, label, cls] of BADGES) {
    if (exts.includes(ext)) return { label, cls };
  }
  return { label: "TXT", cls: "txt" };
}

function humanSize(bytes) {
  if (!bytes) return "";
  const units = ["B", "KB", "MB", "GB"];
  let n = bytes, u = 0;
  while (n >= 1024 && u < units.length - 1) { n /= 1024; u++; }
  return `${n < 10 && u > 0 ? n.toFixed(1) : Math.round(n)} ${units[u]}`;
}

export function initUpload({ onUploaded }) {
  const modal = qs("#uploadModal");
  const fileInput = qs("#fileInput");
  const dropzone = qs("#dropzone");
  const uploader = qs("#uploader");
  const preview = qs("#filePreview");
  const submit = qs("#uploadSubmit");
  const form = qs("#uploadForm");

  // Keep the native picker's filter in sync with what we actually support.
  fileInput.accept = SUPPORTED_EXTS.join(",");

  const open = () => { modal.hidden = false; };
  const close = () => { modal.hidden = true; reset(); };

  function reset() {
    form.reset();
    clearFile();
  }

  function clearFile() {
    fileInput.value = "";
    preview.hidden = true;
    dropzone.hidden = false;
    submit.disabled = true;
  }

  function setFile(file) {
    if (!file) return;
    if (!isSupported(file.name)) {
      toast(`Can't read ${extOf(file.name) || "that file"} — pick a PDF, Office, image, or text file.`);
      return;
    }
    const dt = new DataTransfer();
    dt.items.add(file);
    fileInput.files = dt.files;

    const badge = badgeFor(file.name);
    const badgeEl = qs("#fileBadge");
    badgeEl.textContent = badge.label;
    badgeEl.className = `file-badge file-badge--${badge.cls}`;
    qs("#fileName").textContent = file.name;
    qs("#fileSize").textContent = humanSize(file.size);

    dropzone.hidden = true;
    preview.hidden = false;
    submit.disabled = false;
  }

  qs("#uploadBtn").addEventListener("click", open);
  qs("#uploadClose").addEventListener("click", close);
  modal.addEventListener("click", (e) => { if (e.target === modal) close(); });
  qs("#fileRemove").addEventListener("click", clearFile);

  fileInput.addEventListener("change", () => setFile(fileInput.files[0]));

  // Drag & drop lands anywhere on the uploader, in either state.
  ["dragover", "dragenter"].forEach((evt) =>
    uploader.addEventListener(evt, (e) => { e.preventDefault(); dropzone.classList.add("dragover"); }),
  );
  ["dragleave", "drop"].forEach((evt) =>
    uploader.addEventListener(evt, () => dropzone.classList.remove("dragover")),
  );
  uploader.addEventListener("drop", (e) => { e.preventDefault(); setFile(e.dataTransfer.files[0]); });

  // "How it works" info modal.
  const infoModal = qs("#infoModal");
  const openInfo = () => { infoModal.hidden = false; };
  const closeInfo = () => { infoModal.hidden = true; };
  qs("#uploadInfo").addEventListener("click", openInfo);
  qs("#infoClose").addEventListener("click", closeInfo);
  infoModal.addEventListener("click", (e) => { if (e.target === infoModal) closeInfo(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !infoModal.hidden) closeInfo(); });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!fileInput.files[0]) return;
    submit.disabled = true;
    submit.textContent = "Adding…";
    try {
      const data = new FormData();
      data.append("file", fileInput.files[0]);
      data.append("classification", qs("#clsSelect").value);
      data.append("location", qs("#uploadLocation").value);
      data.append("category", qs("#catSelect").value);
      const doc = await uploadDocument(data, getWorkspaceScope());
      if (doc.status === "pending") {
        const why = doc.pii_findings ? " — possible personal data detected" : "";
        toast(`"${doc.title}" submitted for review${why}`);
      } else {
        toast(`Added "${doc.title}" (${doc.chunks} chunks)`);
      }
      close();
      onUploaded();
    } catch (err) {
      toast(err.message);
      submit.disabled = false;
    } finally {
      submit.textContent = "Add to the brain";
    }
  });
}
