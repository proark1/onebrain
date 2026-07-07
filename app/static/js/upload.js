// Upload modal: choose a file, set its label, add it to the brain.

import { uploadDocument } from "./api.js";
import { qs, toast } from "./dom.js";

export function initUpload({ onUploaded }) {
  const modal = qs("#uploadModal");
  const fileInput = qs("#fileInput");
  const dropzone = qs("#dropzone");
  const dropText = qs("#dropText");
  const submit = qs("#uploadSubmit");
  const form = qs("#uploadForm");

  const open = () => { modal.hidden = false; };
  const close = () => { modal.hidden = true; reset(); };

  function reset() {
    form.reset();
    fileInput.value = "";
    dropText.textContent = "Drop a file here, or click to choose";
    dropzone.classList.remove("has-file");
    submit.disabled = true;
  }

  function setFile(file) {
    if (!file) return;
    const dt = new DataTransfer();
    dt.items.add(file);
    fileInput.files = dt.files;
    dropText.textContent = file.name;
    dropzone.classList.add("has-file");
    submit.disabled = false;
  }

  qs("#uploadBtn").addEventListener("click", open);
  qs("#uploadClose").addEventListener("click", close);
  modal.addEventListener("click", (e) => { if (e.target === modal) close(); });

  fileInput.addEventListener("change", () => setFile(fileInput.files[0]));

  ["dragover", "dragenter"].forEach((evt) =>
    dropzone.addEventListener(evt, (e) => { e.preventDefault(); dropzone.classList.add("dragover"); }),
  );
  ["dragleave", "drop"].forEach((evt) =>
    dropzone.addEventListener(evt, () => dropzone.classList.remove("dragover")),
  );
  dropzone.addEventListener("drop", (e) => { e.preventDefault(); setFile(e.dataTransfer.files[0]); });

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
      const doc = await uploadDocument(data);
      toast(`Added "${doc.title}" (${doc.chunks} chunks)`);
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
