/** 知识库 = 服务器文件库中的一个子目录：PDF 与各版本 Markdown 都保存在服务端。 */
import type { ChunkOptions, ConvertOptions } from "@/lib/api"

export interface ProjectFile {
  /** 相对知识库根目录的路径，如 `合同/2024/a.pdf` */
  path: string
  size: number
  /** 已有对应的 Markdown 产物 */
  converted: boolean
}

export interface MarkdownFile {
  path: string
  pdfPath: string
  version: number
}

export interface Project {
  /** 知识库名称，同时是服务端目录名 */
  name: string
  files: ProjectFile[]
  artifacts: MarkdownFile[]
  /** 批量任务中排队或解析中的 PDF → 状态 */
  active: Record<string, "queued" | "running">
  fastgpt: FastgptState
}

export interface FastgptImport {
  version: number
  md_path: string
  imported: number
  chunks: number | null
  error?: string | null
  md_etag?: string
}

export interface FastgptState {
  /** 服务端是否配置了 FastGPT 地址与密钥 */
  configured: boolean
  dataset: { id: string; name: string } | null
  imports: Record<string, FastgptImport>
  /** 有新版本、导入后又修改过、或上次失败的 PDF */
  pending: string[]
}

export interface FastgptDataset {
  id: string
  name: string
  type: string
  vector_model?: string | null
}

export interface ImportResult {
  pdf_path: string
  status: "imported" | "unchanged" | "conflict" | "skipped" | "error" | "queued" | "running" | "cancelled"
  md_path?: string
  chunks?: number | null
  replaced?: number
  message?: string | null
}

export interface ImportJob {
  id: string
  status: "running" | "done"
  total: number
  counts: Partial<Record<ImportResult["status"], number>>
  items: ImportResult[]
}

export type ConflictPolicy = "ask" | "replace" | "keep" | "skip"

const EMPTY_FASTGPT: FastgptState = { configured: false, dataset: null, imports: {}, pending: [] }

export type BatchItemStatus = "queued" | "running" | "done" | "error" | "skipped" | "cancelled"

export interface BatchItem {
  path: string
  status: BatchItemStatus
  version_path?: string
  pages?: number
  chunks?: number
  ocr_pages?: number
  error?: string | null
  message?: string | null
}

export interface Batch {
  id: string
  created: number
  updated: number
  status: "running" | "done" | "cancelled"
  total: number
  counts: Record<BatchItemStatus, number>
  skip_converted: boolean
  items?: BatchItem[]
}

export type ExportMode = "latest" | "all" | "fastgpt"

export interface KbSummary {
  name: string
  pdfs: number
  converted: number
  updated: number
}

export interface KbSettings {
  convert: ConvertOptions
  chunk: ChunkOptions
}

export interface VersionMeta {
  origin?: string
  convert?: ConvertOptions
  chunk?: ChunkOptions
}

export interface UploadResult {
  path: string
  status: "added" | "renamed" | "skipped" | "error"
  message?: string
}

export interface UploadItem {
  file: File
  /** 保留文件夹结构的相对路径；缺省时使用文件名 */
  path?: string
}

export const isPdfName = (n: string) => /\.pdf$/i.test(n)
export const mdName = (pdf: string, version: number) => `${pdf.replace(/\.pdf$/i, "")}_v${version}.md`

const byPath = (a: { path: string }, b: { path: string }) => a.path.localeCompare(b.path, "zh-CN", { numeric: true })
export const sortByPath = <T extends { path: string }>(items: T[]) => [...items].sort(byPath)

/** 按知识库和当前会话中已有的最大版本号递增；保存时服务端会在该号被占用时顺延。 */
export function nextMarkdownFile(pdfPath: string, artifacts: MarkdownFile[]): MarkdownFile {
  const versions = artifacts.filter((file) => file.pdfPath.toLowerCase() === pdfPath.toLowerCase()).map((file) => file.version)
  const version = Math.max(0, ...versions) + 1
  return { path: mdName(pdfPath, version), pdfPath, version }
}

async function json<T>(resp: Response): Promise<T> {
  const data = await resp.json().catch(() => ({}))
  if (!resp.ok) throw new Error((data as { detail?: string }).detail || resp.statusText)
  return data as T
}

const kbUrl = (kb: string, rest = "") => `api/kbs/${encodeURIComponent(kb)}${rest}`
const send = (method: string, body: unknown) => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
})

// ---------------------------------------------------------------- 知识库

export async function listKbs() {
  return json<{ kbs: KbSummary[]; storage: { path: string; writable: boolean } }>(await fetch("api/kbs"))
}

export async function createKb(name: string) {
  return json<{ name: string }>(await fetch("api/kbs", send("POST", { name })))
}

interface RawInventory {
  name: string
  files: ProjectFile[]
  artifacts: { path: string; pdf_path: string; version: number }[]
  active?: Project["active"]
  fastgpt?: FastgptState
}

export async function openKb(name: string): Promise<Project> {
  const raw = await json<RawInventory>(await fetch(kbUrl(name, "/files")))
  return {
    name: raw.name,
    files: raw.files.map(({ path, size, converted }) => ({ path, size, converted })),
    artifacts: raw.artifacts.map((a) => ({ path: a.path, pdfPath: a.pdf_path, version: a.version })),
    active: raw.active ?? {},
    fastgpt: raw.fastgpt ?? EMPTY_FASTGPT,
  }
}

export const rescan = (project: Project) => openKb(project.name)

export async function getSettings(kb: string) {
  return json<KbSettings>(await fetch(kbUrl(kb, "/settings")))
}

export async function putSettings(kb: string, settings: KbSettings) {
  return json<KbSettings>(await fetch(kbUrl(kb, "/settings"), send("PUT", settings)))
}

// ---------------------------------------------------------------- 文件

export const pdfUrl = (kb: string, path: string, page?: number | null) =>
  `${kbUrl(kb, "/pdf")}?path=${encodeURIComponent(path)}${page ? `#page=${page}` : ""}`

export async function readMarkdown(kb: string, path: string) {
  return json<{ path: string; markdown: string; etag: string; meta: VersionMeta }>(
    await fetch(`${kbUrl(kb, "/markdown")}?path=${encodeURIComponent(path)}`))
}

export async function saveVersion(kb: string, body: {
  pdf_path: string; markdown: string; version?: number
  job_id?: string | null; convert?: ConvertOptions | null; chunk?: ChunkOptions
}) {
  return json<{ path: string; pdf_path: string; version: number; etag: string }>(
    await fetch(kbUrl(kb, "/versions"), send("POST", body)))
}

export async function overwriteMarkdown(kb: string, path: string, markdown: string, etag?: string) {
  return json<{ path: string; etag: string }>(await fetch(kbUrl(kb, "/markdown"), send("PUT", { path, markdown, etag })))
}

export async function startKbJob(kb: string, path: string, options: ConvertOptions & ChunkOptions) {
  return json<{ id: string; page_count: number }>(await fetch(kbUrl(kb, "/jobs"), send("POST", { path, options })))
}

// ---------------------------------------------------------------- 批量解析与打包下载

export async function createBatch(kb: string, paths: string[], settings: KbSettings, skipConverted: boolean) {
  return json<Batch>(await fetch(kbUrl(kb, "/batches"), send("POST", { paths, settings, skip_converted: skipConverted })))
}

export async function listBatches(kb: string) {
  return (await json<{ batches: Batch[] }>(await fetch(kbUrl(kb, "/batches")))).batches
}

export async function getBatch(kb: string, id: string) {
  return json<Batch>(await fetch(kbUrl(kb, `/batches/${id}`)))
}

export async function cancelBatch(kb: string, id: string) {
  return json<Batch>(await fetch(kbUrl(kb, `/batches/${id}/cancel`), { method: "POST" }))
}

export async function retryBatch(kb: string, id: string) {
  return json<Batch>(await fetch(kbUrl(kb, `/batches/${id}/retry`), { method: "POST" }))
}

/** 服务端先打包，再用普通链接下载，大文件也不占浏览器内存。 */
export async function downloadZip(kb: string, body: { mode: ExportMode; paths?: string[]; with_pdf?: boolean; batch_id?: string }) {
  const info = await json<{ id: string; name: string; markdown: number; pdf: number }>(
    await fetch(kbUrl(kb, "/exports"), send("POST", body)))
  const a = document.createElement("a")
  a.href = kbUrl(kb, `/exports/${info.id}`)
  a.download = info.name
  document.body.appendChild(a)
  a.click()
  a.remove()
  return info
}

// ---------------------------------------------------------------- FastGPT

export async function listDatasets() {
  return (await json<{ datasets: FastgptDataset[] }>(await fetch("api/fastgpt/datasets"))).datasets
}

export async function bindDataset(kb: string, datasetId: string | null) {
  return json<FastgptState>(await fetch(kbUrl(kb, "/fastgpt"), send("PUT", { dataset_id: datasetId })))
}

export async function importToFastgpt(kb: string, body: { pdf_path: string; md_path?: string; on_conflict?: ConflictPolicy; force?: boolean }) {
  return json<ImportResult>(await fetch(kbUrl(kb, "/fastgpt/import"), send("POST", body)))
}

export async function startImportJob(kb: string, paths: string[], onConflict: ConflictPolicy) {
  return json<ImportJob>(await fetch(kbUrl(kb, "/fastgpt/imports"), send("POST", { paths, on_conflict: onConflict })))
}

export async function getImportJob(kb: string, id: string) {
  return json<ImportJob>(await fetch(kbUrl(kb, `/fastgpt/imports/${id}`)))
}

// ---------------------------------------------------------------- 上传

const BATCH_BYTES = 64 << 20
const BATCH_FILES = 20

function postForm(url: string, form: FormData, onProgress: (loaded: number) => void): Promise<{ results: UploadResult[] }> {
  // fetch 无法报告上传进度，这里用 XHR
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open("POST", url)
    xhr.upload.onprogress = (e) => onProgress(e.loaded)
    xhr.onload = () => {
      let data: { detail?: string; results?: UploadResult[] } = {}
      try {
        data = JSON.parse(xhr.responseText)
      } catch { /* 非 JSON 响应 */ }
      if (xhr.status >= 200 && xhr.status < 300 && data.results) resolve({ results: data.results })
      else reject(new Error(data.detail || `上传失败（HTTP ${xhr.status}）`))
    }
    xhr.onerror = () => reject(new Error("网络错误，上传中断"))
    xhr.send(form)
  })
}

/** 分批上传，避免一次请求过大；onProgress 报告已上传字节数与总字节数。 */
export async function uploadFiles(kb: string, items: UploadItem[], onProgress?: (loaded: number, total: number) => void) {
  const total = items.reduce((n, it) => n + it.file.size, 0)
  const results: UploadResult[] = []
  let sent = 0
  let i = 0
  while (i < items.length) {
    const batch: UploadItem[] = []
    let bytes = 0
    while (i < items.length && batch.length < BATCH_FILES && (batch.length === 0 || bytes + items[i].file.size <= BATCH_BYTES)) {
      bytes += items[i].file.size
      batch.push(items[i++])
    }
    const form = new FormData()
    for (const it of batch) {
      form.append("files", it.file, it.file.name)
      form.append("paths", it.path || it.file.name)
    }
    try {
      const data = await postForm(kbUrl(kb, "/files"), form, (loaded) => onProgress?.(sent + loaded, total))
      results.push(...data.results)
    } catch (e) {
      const message = e instanceof Error ? e.message : String(e)
      results.push(...batch.map((it) => ({ path: it.path || it.file.name, status: "error" as const, message })))
    }
    sent += bytes
    onProgress?.(sent, total)
  }
  return results
}

/** 文件选择框给出的文件；webkitdirectory 选择时保留相对路径（去掉所选目录这一层之外的结构保持不变）。 */
export function itemsFromInput(list: FileList | File[]): UploadItem[] {
  return Array.from(list)
    .filter((f) => isPdfName(f.name))
    .map((file) => ({ file, path: file.webkitRelativePath || file.name }))
}

type Entry = FileSystemEntry & {
  isFile: boolean
  isDirectory: boolean
  file?: (ok: (f: File) => void, err: (e: unknown) => void) => void
  createReader?: () => { readEntries: (ok: (e: Entry[]) => void, err: (e: unknown) => void) => void }
}

async function walkEntry(entry: Entry, prefix: string, out: UploadItem[]) {
  if (entry.isFile && entry.file) {
    if (!isPdfName(entry.name)) return
    const file = await new Promise<File>((ok, err) => entry.file!(ok, err))
    out.push({ file, path: prefix + entry.name })
  } else if (entry.isDirectory && entry.createReader) {
    const reader = entry.createReader()
    // readEntries 每次最多返回约 100 项，需要循环读到空
    for (;;) {
      const batch = await new Promise<Entry[]>((ok, err) => reader.readEntries(ok, err))
      if (!batch.length) break
      for (const child of batch) await walkEntry(child, `${prefix}${entry.name}/`, out)
    }
  }
}

/** 拖放的文件和文件夹：文件夹递归展开，保留目录结构，只保留 PDF。 */
export async function itemsFromDrop(dt: DataTransfer): Promise<UploadItem[]> {
  const entries = Array.from(dt.items ?? [])
    .map((it) => (it.kind === "file" ? (it.webkitGetAsEntry?.() as Entry | null) : null))
    .filter((e): e is Entry => !!e)
  if (!entries.length) return itemsFromInput(dt.files)
  const out: UploadItem[] = []
  for (const entry of entries) await walkEntry(entry, "", out)
  return out
}

export function summarizeUpload(results: UploadResult[]) {
  const count = (s: UploadResult["status"]) => results.filter((r) => r.status === s).length
  const parts = [
    count("added") + count("renamed") ? `新增 ${count("added") + count("renamed")} 个` : "",
    count("skipped") ? `跳过 ${count("skipped")} 个（内容相同）` : "",
    count("error") ? `失败 ${count("error")} 个` : "",
  ].filter(Boolean)
  return parts.join("，") || "没有可上传的 PDF"
}
