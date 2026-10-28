// Dataset packages for the hub: build the ZIP the upstream create-dataset skill
// produces (<id>/dataset.json, <id>/README.md, <id>/data/...) and upload it in parts.

import { Zip, ZipDeflate, ZipPassThrough } from "fflate";

import { api, ApiError } from "@/api/client";
import type { Dataset } from "@/api/types";

export interface DatasetMeta {
  id: string;
  name: string;
  description: string;
  category: string;
  version: string;
  tags: string[];
  author: string;
  license: string;
}

export interface PackageFile {
  /** Path inside data/ (keeps folder structure for folder uploads). */
  path: string;
  file: File;
}

// Formats that are already compressed are stored as-is.
const STORED = /\.(zip|gz|tgz|bz2|xz|zst|7z|png|jpe?g|gif|webp|mp4|mp3|parquet|feather|pdf)$/i;

/** Packages built in the browser stay below this; larger datasets go through the create-dataset tool. */
export const MAX_BROWSER_PACKAGE_BYTES = 2 * 1024 ** 3;

export const SLUG = /^[a-z0-9][a-z0-9_-]{1,99}$/;

export function slugify(name: string): string {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 80);
}

export function readmeTemplate(meta: Pick<DatasetMeta, "name" | "description">): string {
  return `# ${meta.name || "Dataset"}

${meta.description || "What this dataset contains and how it was collected."}

## Files

| File | Description |
| --- | --- |
| data/… | … |

## Columns

Describe each column, its type and unit.

## Provenance and license

Where the data comes from, how it was processed, and the terms of use.
`;
}

/** Stream files through fflate into a ZIP blob (input is read in chunks, never all at once). */
export async function buildPackage(meta: DatasetMeta, readme: string, files: PackageFile[], onProgress?: (fraction: number) => void): Promise<Blob> {
  const chunks: Uint8Array[] = [];
  let failure: Error | null = null;
  let resolveDone!: () => void;
  const done = new Promise<void>((resolve) => (resolveDone = resolve));
  const zip = new Zip((err, chunk, final) => {
    if (err) {
      failure = err;
      resolveDone();
      return;
    }
    chunks.push(chunk);
    if (final) resolveDone();
  });
  const encoder = new TextEncoder();
  const addText = (path: string, text: string) => {
    const entry = new ZipDeflate(path, { level: 6 });
    zip.add(entry);
    entry.push(encoder.encode(text), true);
  };
  const root = meta.id;
  addText(`${root}/dataset.json`, `${JSON.stringify(meta, null, 2)}\n`);
  addText(`${root}/README.md`, readme);
  const total = files.reduce((n, f) => n + f.file.size, 0) || 1;
  let read = 0;
  for (const { path, file } of files) {
    const name = `${root}/data/${path.replace(/^\/+/, "")}`;
    const entry = STORED.test(name) ? new ZipPassThrough(name) : new ZipDeflate(name, { level: 6 });
    zip.add(entry);
    const reader = file.stream().getReader();
    for (;;) {
      const { value, done: eof } = await reader.read();
      if (eof) break;
      entry.push(value);
      read += value.length;
      onProgress?.(read / total);
      if (failure) throw failure;
    }
    entry.push(new Uint8Array(0), true);
  }
  zip.end();
  await done;
  if (failure) throw failure;
  return new Blob(chunks as BlobPart[], { type: "application/zip" });
}

interface InitUpload {
  upload_id: string;
  part_size: number;
  part_urls: { part_number: number; upload_url: string }[];
}

/**
 * Upload a package in parts. URLs are pre-signed (local blob store or S3), so the
 * parts are sent without the session's Authorization header.
 */
export async function uploadPackage(datasetId: string, orgId: string, blob: Blob, onProgress?: (fraction: number) => void): Promise<Dataset> {
  const init = await api.post<InitUpload>(`/api/user/data/datasets/${datasetId}/init-upload`, { file_size: blob.size }, { org_id: orgId });
  let sent = 0;
  const queue = [...init.part_urls];
  const worker = async () => {
    for (let part = queue.shift(); part; part = queue.shift()) {
      const start = (part.part_number - 1) * init.part_size;
      const body = blob.slice(start, start + init.part_size);
      const r = await fetch(part.upload_url, { method: "PUT", body });
      if (!r.ok) throw new ApiError(r.status, `Uploading part ${part.part_number} failed (${r.status})`);
      sent += body.size;
      onProgress?.(sent / blob.size);
    }
  };
  await Promise.all(Array.from({ length: Math.min(3, queue.length) }, worker));
  return api.post<Dataset>(`/api/user/data/datasets/${datasetId}/complete-upload`, { upload_id: init.upload_id, parts: [] }, { org_id: orgId });
}
