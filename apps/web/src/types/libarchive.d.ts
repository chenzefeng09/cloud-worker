// Minimal typings for libarchive.js (the package ships no .d.ts). We only use
// the read/list surface: open an archive, list entries (metadata only — no
// extraction), and check for encryption. See ArchivePreview.tsx.
declare module 'libarchive.js' {
  export interface LibarchiveCompressedFile {
    /** Entry base name (last path segment). */
    readonly name: string;
    /** Uncompressed size in bytes, from the archive metadata (no extraction). */
    readonly size: number;
    readonly lastModified: number;
    /** Materialise this single entry's content (NOT called for listing). */
    extract(): Promise<File>;
  }
  export interface LibarchiveEntry {
    file: LibarchiveCompressedFile;
    /** Directory prefix of the entry, e.g. "docs/" ("" at the archive root). */
    path: string;
  }
  export class Archive {
    static init(options?: { workerUrl?: string }): unknown;
    static open(file: File | Blob): Promise<Archive>;
    /** True when the archive holds encrypted data (contents and/or headers). */
    hasEncryptedData(): Promise<boolean | null>;
    /** List entries as metadata (name/size/path) — does not extract content. */
    getFilesArray(): Promise<LibarchiveEntry[]>;
    close(): Promise<void>;
  }
}
