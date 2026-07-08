"use client";

import { useMemo, useRef, useState, type DragEvent, type FormEvent } from "react";
import {
  approveDocument,
  listDocuments,
  listPendingDocuments,
  uploadDocument,
} from "@/lib/onebrain-client";
import type { DocumentSummary, PendingDocument } from "@/lib/onebrain-types";

type DocumentsPanelProps = {
  initialDocuments: DocumentSummary[];
  initialError?: string;
  initialPending: PendingDocument[];
  pendingReviewAvailable: boolean;
};

const SUPPORTED_EXTS = [
  ".pdf", ".docx", ".xlsx", ".xlsm", ".pptx", ".rtf",
  ".txt", ".md", ".markdown", ".csv", ".tsv", ".json",
  ".html", ".htm", ".xml", ".yaml", ".yml", ".log",
  ".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp", ".gif", ".webp",
];

const CLASSIFICATION_OPTIONS = [
  { label: "Public", value: "public" },
  { label: "Internal", value: "internal" },
  { label: "Confidential", value: "confidential" },
  { label: "Restricted", value: "restricted" },
];

const LOCATION_OPTIONS = [
  { label: "Global", value: "global" },
  { label: "Munich", value: "munich" },
  { label: "Berlin", value: "berlin" },
  { label: "Hamburg", value: "hamburg" },
];

const CATEGORY_OPTIONS = [
  { label: "General", value: "general" },
  { label: "Customer service", value: "cs" },
  { label: "Operations", value: "ops" },
  { label: "HR", value: "hr" },
  { label: "Finance", value: "finance" },
  { label: "Marketing", value: "marketing" },
];

const ACCEPTED_FILES = SUPPORTED_EXTS.join(",");

function extOf(name: string): string {
  const index = name.lastIndexOf(".");
  return index < 0 ? "" : name.slice(index).toLowerCase();
}

function isSupportedFile(name: string): boolean {
  return SUPPORTED_EXTS.includes(extOf(name));
}

function humanSize(bytes: number): string {
  if (!bytes) {
    return "";
  }
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value < 10 && unit > 0 ? value.toFixed(1) : Math.round(value)} ${units[unit]}`;
}

function labelFor(value: string): string {
  return value.replace(/_/g, " ");
}

function classificationClass(value: string): string {
  return ["public", "internal", "confidential", "restricted"].includes(value) ? value : "internal";
}

export function DocumentsPanel({
  initialDocuments,
  initialError = "",
  initialPending,
  pendingReviewAvailable,
}: DocumentsPanelProps) {
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [documents, setDocuments] = useState<DocumentSummary[]>(initialDocuments);
  const [pending, setPending] = useState<PendingDocument[]>(initialPending);
  const [canReview, setCanReview] = useState(pendingReviewAvailable);
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [classification, setClassification] = useState("internal");
  const [location, setLocation] = useState("global");
  const [category, setCategory] = useState("general");
  const [loading, setLoading] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [approvingId, setApprovingId] = useState("");
  const [error, setError] = useState(initialError);
  const [notice, setNotice] = useState("");

  const documentStats = useMemo(() => {
    const chunks = documents.reduce((total, document) => total + document.chunks, 0);
    const piiFindings = documents.reduce((total, document) => total + document.pii_findings, 0);
    return { chunks, piiFindings };
  }, [documents]);

  async function refresh() {
    setLoading(true);
    setError("");
    try {
      const [nextDocuments, nextPending] = await Promise.all([
        listDocuments(),
        listPendingDocuments()
          .then((items) => ({ available: true, items }))
          .catch(() => ({ available: false, items: [] as PendingDocument[] })),
      ]);
      setDocuments(nextDocuments);
      setPending(nextPending.items);
      setCanReview(nextPending.available);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not refresh documents.");
    } finally {
      setLoading(false);
    }
  }

  function chooseFile(file: File | null) {
    setNotice("");
    setError("");
    if (!file) {
      setSelectedFile(null);
      return;
    }
    if (!isSupportedFile(file.name)) {
      setError(`OneBrain cannot read ${extOf(file.name) || "that file"} here. Choose a PDF, Office, image, or text file.`);
      return;
    }
    setSelectedFile(file);
  }

  function removeFile() {
    setSelectedFile(null);
    if (fileInputRef.current) {
      fileInputRef.current.value = "";
    }
  }

  function onDrop(event: DragEvent<HTMLLabelElement>) {
    event.preventDefault();
    chooseFile(event.dataTransfer.files[0] ?? null);
  }

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selectedFile || uploading) {
      return;
    }
    setUploading(true);
    setError("");
    setNotice("");
    try {
      const document = await uploadDocument({
        category,
        classification,
        file: selectedFile,
        location,
      });
      const reviewNote = document.status === "pending" ? " submitted for review" : " added";
      const piiNote = document.pii_findings ? " with possible personal data detected" : "";
      setNotice(`${document.title}${reviewNote}${piiNote}.`);
      removeFile();
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Upload failed.");
    } finally {
      setUploading(false);
    }
  }

  async function onApprove(document: PendingDocument) {
    setApprovingId(document.doc_id);
    setError("");
    setNotice("");
    try {
      await approveDocument(document.doc_id);
      setNotice(`${document.title} approved.`);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Approval failed.");
    } finally {
      setApprovingId("");
    }
  }

  return (
    <div className="documentsWorkspace">
      <header className="documentsTopbar">
        <div>
          <p className="eyebrow">Knowledge base</p>
          <h1>Documents</h1>
        </div>
        <button className="secondaryButton" disabled={loading} type="button" onClick={() => void refresh()}>
          {loading ? "Refreshing" : "Refresh"}
        </button>
      </header>

      {error ? <div className="inlineError">{error}</div> : null}
      {notice ? <div className="inlineNotice">{notice}</div> : null}

      <section className="documentStats" aria-label="Document summary">
        <div>
          <strong>{documents.length}</strong>
          <span>visible</span>
        </div>
        <div>
          <strong>{documentStats.chunks}</strong>
          <span>chunks</span>
        </div>
        <div>
          <strong>{pending.length}</strong>
          <span>pending</span>
        </div>
        <div>
          <strong>{documentStats.piiFindings}</strong>
          <span>PII flags</span>
        </div>
      </section>

      <div className="documentsGrid">
        <section className="documentLibrary" aria-labelledby="documentLibraryTitle">
          <div className="panelHead">
            <div>
              <p className="eyebrow">Approved knowledge</p>
              <h2 id="documentLibraryTitle">Visible documents</h2>
            </div>
            <span>{documents.length}</span>
          </div>

          <div className="documentList">
            {documents.length === 0 ? (
              <div className="emptyPanel">
                <h3>No documents visible</h3>
                <p>Upload a document or switch to a workspace with approved knowledge.</p>
              </div>
            ) : null}
            {documents.map((document) => (
              <DocumentRow document={document} key={document.doc_id} />
            ))}
          </div>
        </section>

        <aside className="documentSide">
          <section className="uploadPanel" aria-labelledby="uploadTitle">
            <div className="panelHead">
              <div>
                <p className="eyebrow">Ingest</p>
                <h2 id="uploadTitle">Upload document</h2>
              </div>
            </div>
            <form className="uploadForm" onSubmit={(event) => void onSubmit(event)}>
              <label
                className={selectedFile ? "dropzone hasFile" : "dropzone"}
                htmlFor="documentFile"
                onDragOver={(event) => event.preventDefault()}
                onDrop={onDrop}
              >
                <input
                  accept={ACCEPTED_FILES}
                  id="documentFile"
                  hidden
                  onChange={(event) => chooseFile(event.target.files?.[0] ?? null)}
                  ref={fileInputRef}
                  type="file"
                />
                {selectedFile ? (
                  <span className="selectedFile">
                    <span>{selectedFile.name}</span>
                    <small>{humanSize(selectedFile.size)}</small>
                  </span>
                ) : (
                  <>
                    <span className="dropzoneTitle">Drop a file or browse</span>
                    <small>PDF, Office, image, and text formats</small>
                  </>
                )}
              </label>

              {selectedFile ? (
                <button className="textButton" type="button" onClick={removeFile}>
                  Remove file
                </button>
              ) : null}

              <div className="uploadControls">
                <SelectField label="Classification" value={classification} options={CLASSIFICATION_OPTIONS} onChange={setClassification} />
                <SelectField label="Location" value={location} options={LOCATION_OPTIONS} onChange={setLocation} />
                <SelectField label="Category" value={category} options={CATEGORY_OPTIONS} onChange={setCategory} />
              </div>

              <p className="uploadNote">Labels decide who can retrieve the file after approval.</p>
              <button className="primaryButton" disabled={!selectedFile || uploading} type="submit">
                {uploading ? "Uploading" : "Upload"}
              </button>
            </form>
          </section>

          {canReview ? (
            <section className="reviewPanel" aria-labelledby="reviewTitle">
              <div className="panelHead">
                <div>
                  <p className="eyebrow">Review</p>
                  <h2 id="reviewTitle">Pending approval</h2>
                </div>
                <span>{pending.length}</span>
              </div>
              <div className="pendingList">
                {pending.length === 0 ? <p className="mutedLine">No documents waiting for review.</p> : null}
                {pending.map((document) => (
                  <PendingRow
                    approving={approvingId === document.doc_id}
                    document={document}
                    key={document.doc_id}
                    onApprove={onApprove}
                  />
                ))}
              </div>
            </section>
          ) : null}
        </aside>
      </div>
    </div>
  );
}

function SelectField({
  label,
  onChange,
  options,
  value,
}: {
  label: string;
  onChange: (value: string) => void;
  options: Array<{ label: string; value: string }>;
  value: string;
}) {
  return (
    <label className="field">
      <span className="fieldLabel">{label}</span>
      <select className="select" value={value} onChange={(event) => onChange(event.target.value)}>
        {options.map((option) => (
          <option key={option.value} value={option.value}>{option.label}</option>
        ))}
      </select>
    </label>
  );
}

function DocumentRow({ document }: { document: DocumentSummary }) {
  const classification = classificationClass(document.classification);
  return (
    <article className={`documentRow class-${classification}`}>
      <span className="classificationRail" aria-hidden="true" />
      <div className="documentInfo">
        <h3>{document.title}</h3>
        <p>
          {labelFor(document.classification)} / {labelFor(document.category)} / {labelFor(document.location)}
        </p>
      </div>
      <div className="documentMeta">
        <span>{document.chunks} chunks</span>
        <span>{document.status}</span>
        {document.pii_findings ? <span className="dangerText">{document.pii_findings} PII</span> : null}
      </div>
    </article>
  );
}

function PendingRow({
  approving,
  document,
  onApprove,
}: {
  approving: boolean;
  document: PendingDocument;
  onApprove: (document: PendingDocument) => Promise<void>;
}) {
  const classification = classificationClass(document.classification);
  return (
    <article className={`pendingRow class-${classification}`}>
      <span className="classificationRail" aria-hidden="true" />
      <div className="documentInfo">
        <h3>{document.title}</h3>
        <p>
          {labelFor(document.classification)} / {labelFor(document.category)} / {labelFor(document.location)}
        </p>
        {document.has_pii ? <span className="dangerText">Possible personal data</span> : null}
      </div>
      <button className="reviewButton" disabled={approving} type="button" onClick={() => void onApprove(document)}>
        {approving ? "Approving" : "Approve"}
      </button>
    </article>
  );
}
