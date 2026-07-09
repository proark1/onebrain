"use client";

import { useCallback, useEffect, useMemo, useRef, useState, type DragEvent, type FormEvent } from "react";
import {
  approveDocument,
  listDocuments,
  listPendingDocuments,
  uploadDocument,
} from "@/lib/onebrain-client";
import { MetricStrip, Notice, PageHeader, Panel } from "@/components/admin-ui";
import { useWorkspace } from "@/components/workspace-provider";
import type { DocumentSummary, PendingDocument } from "@/lib/onebrain-types";

type DocumentsPanelProps = {
  initialDocuments: DocumentSummary[];
  initialError?: string;
  initialPending: PendingDocument[];
  pendingReviewAvailable: boolean;
};

type DocumentAction = "library" | "upload" | "review";

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
  const { scope } = useWorkspace();
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
  const [query, setQuery] = useState("");
  const [filterClassification, setFilterClassification] = useState("");
  const [filterCategory, setFilterCategory] = useState("");
  const [filterLocation, setFilterLocation] = useState("");
  const [filterStatus, setFilterStatus] = useState("");
  const [activeAction, setActiveAction] = useState<DocumentAction>("library");

  const documentStats = useMemo(() => {
    const chunks = documents.reduce((total, document) => total + document.chunks, 0);
    const piiFindings = documents.reduce((total, document) => total + document.pii_findings, 0);
    return { chunks, piiFindings };
  }, [documents]);
  const visibleDocuments = useMemo(() => {
    const normalizedQuery = query.trim().toLowerCase();
    return documents.filter((document) => {
      if (filterClassification && document.classification !== filterClassification) {
        return false;
      }
      if (filterCategory && document.category !== filterCategory) {
        return false;
      }
      if (filterLocation && document.location !== filterLocation) {
        return false;
      }
      if (filterStatus && document.status !== filterStatus) {
        return false;
      }
      if (!normalizedQuery) {
        return true;
      }
      return [
        document.title,
        document.classification,
        document.category,
        document.location,
        document.status,
      ].some((value) => value.toLowerCase().includes(normalizedQuery));
    });
  }, [documents, filterCategory, filterClassification, filterLocation, filterStatus, query]);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [nextDocuments, nextPending] = await Promise.all([
        listDocuments(scope),
        listPendingDocuments(scope)
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
  }, [scope]);

  useEffect(() => {
    let active = true;
    queueMicrotask(() => {
      if (active) {
        void refresh();
      }
    });
    return () => {
      active = false;
    };
  }, [refresh]);

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
      }, scope);
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
      await approveDocument(document.doc_id, scope);
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
      <PageHeader
        actions={(
          <>
            <button className="secondaryButton" disabled={loading} type="button" onClick={() => void refresh()}>
              {loading ? "Refreshing" : "Refresh"}
            </button>
            {canReview ? (
              <button className="secondaryButton" type="button" onClick={() => setActiveAction("review")}>
                Review {pending.length}
              </button>
            ) : null}
            <button className="primaryButton" type="button" onClick={() => setActiveAction("upload")}>
              Upload
            </button>
          </>
        )}
        eyebrow="Knowledge"
        meta={<span className="scopePill"><span className="statusDot" />Workspace scoped</span>}
        title="Knowledge library"
      />

      {error ? <Notice tone="error">{error}</Notice> : null}
      {notice ? <Notice tone="success">{notice}</Notice> : null}

      <MetricStrip
        metrics={[
          { label: "visible", value: documents.length },
          { label: "chunks", value: documentStats.chunks },
          { label: "pending", tone: pending.length ? "warning" : undefined, value: pending.length },
          { label: "PII flags", tone: documentStats.piiFindings ? "danger" : undefined, value: documentStats.piiFindings },
        ]}
      />

      {activeAction === "upload" ? (
        <Panel
          actions={<button className="secondaryButton" type="button" onClick={() => setActiveAction("library")}>Close</button>}
          eyebrow="Upload"
          title="Add knowledge"
        >
          <form className="uploadForm compactWorkflow" onSubmit={(event) => void onSubmit(event)}>
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

            <div className="uploadControls inlineControls">
              <SelectField label="Classification" value={classification} options={CLASSIFICATION_OPTIONS} onChange={setClassification} />
              <SelectField label="Location" value={location} options={LOCATION_OPTIONS} onChange={setLocation} />
              <SelectField label="Category" value={category} options={CATEGORY_OPTIONS} onChange={setCategory} />
            </div>

            <div className="workflowActions">
              {selectedFile ? (
                <button className="textButton" type="button" onClick={removeFile}>
                  Remove file
                </button>
              ) : <span className="uploadNote">Labels decide who can retrieve the file after approval.</span>}
              <button className="primaryButton" disabled={!selectedFile || uploading} type="submit">
                {uploading ? "Uploading" : "Upload knowledge"}
              </button>
            </div>
          </form>
        </Panel>
      ) : null}

      {activeAction === "review" && canReview ? (
        <Panel
          actions={<button className="secondaryButton" type="button" onClick={() => setActiveAction("library")}>Close</button>}
          count={pending.length}
          eyebrow="Review"
          title="Pending approval"
        >
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
        </Panel>
      ) : null}

      <div className="documentsGrid singleColumn">
        <Panel count={visibleDocuments.length} eyebrow="Approved knowledge" title="Visible documents">
          <div className="filterBar" aria-label="Document filters">
            <label className="field">
              <span className="fieldLabel">Search</span>
              <input className="input" value={query} onChange={(event) => setQuery(event.target.value)} />
            </label>
            <label className="field">
              <span className="fieldLabel">Classification</span>
              <select className="select" value={filterClassification} onChange={(event) => setFilterClassification(event.target.value)}>
                <option value="">All classifications</option>
                {CLASSIFICATION_OPTIONS.map((option) => (
                  <option key={option.value} value={option.value}>{option.label}</option>
                ))}
              </select>
            </label>
            <label className="field">
              <span className="fieldLabel">Category</span>
              <select className="select" value={filterCategory} onChange={(event) => setFilterCategory(event.target.value)}>
                <option value="">All categories</option>
                {CATEGORY_OPTIONS.map((option) => (
                  <option key={option.value} value={option.value}>{option.label}</option>
                ))}
              </select>
            </label>
            <label className="field">
              <span className="fieldLabel">Location</span>
              <select className="select" value={filterLocation} onChange={(event) => setFilterLocation(event.target.value)}>
                <option value="">All locations</option>
                {LOCATION_OPTIONS.map((option) => (
                  <option key={option.value} value={option.value}>{option.label}</option>
                ))}
              </select>
            </label>
            <label className="field">
              <span className="fieldLabel">Status</span>
              <select className="select" value={filterStatus} onChange={(event) => setFilterStatus(event.target.value)}>
                <option value="">All statuses</option>
                <option value="approved">Approved</option>
                <option value="pending">Pending</option>
                <option value="quarantined">Quarantined</option>
              </select>
            </label>
          </div>

          <div className="documentList knowledgeTable">
            {visibleDocuments.length ? (
              <div className="knowledgeTableHead" aria-hidden="true">
                <span>Title</span>
                <span>Access</span>
                <span>Chunks</span>
                <span>Status</span>
                <span>PII</span>
              </div>
            ) : null}
            {visibleDocuments.length === 0 ? (
              <div className="emptyPanel">
                <h3>No matching documents</h3>
                <p>Adjust the filters, upload a document, or switch to a workspace with approved knowledge.</p>
              </div>
            ) : null}
            {visibleDocuments.map((document) => (
              <DocumentRow document={document} key={document.doc_id} />
            ))}
          </div>
        </Panel>
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
      </div>
      <span>{labelFor(document.classification)} / {labelFor(document.category)} / {labelFor(document.location)}</span>
      <div className="documentMeta">
        <span>{document.chunks} chunks</span>
      </div>
      <span>{document.status}</span>
      <span className={document.pii_findings ? "dangerText" : "mutedInline"}>
        {document.pii_findings ? `${document.pii_findings} PII` : "clear"}
      </span>
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
