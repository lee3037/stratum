import { useCallback, useEffect, useRef, useState } from "react"
import { LoaderCircle, Send, Upload } from "lucide-react"

import type { ChunkOptions, ConvertOptions, ParserInfo } from "@/lib/api"
import { DEFAULT_CHUNK, DEFAULT_CONVERT, getJob, rechunk } from "@/lib/api"
import type { MarkdownFile, Project, UploadItem } from "@/lib/library"
import {
  createBatch, getSettings, importToFastgpt, itemsFromDrop, nextMarkdownFile, openKb, overwriteMarkdown, pdfUrl, readMarkdown, rescan,
  saveVersion, startKbJob, summarizeUpload, uploadFiles,
} from "@/lib/library"
import { loadSeparators } from "@/lib/separators"
import { useParsers } from "@/lib/use-parsers"
import { Button } from "@/components/ui/button"
import { TooltipProvider } from "@/components/ui/tooltip"
import { Hint } from "@/components/hint"
import type { MorphButtonState } from "@/components/spectrumui/morph-button"
import { toast, Toaster } from "@/components/toaster"
import { FileDrawer, type FileDrawerView, type ParseSettings } from "@/components/file-drawer"
import { BatchStage } from "@/components/batch-stage"
import { KbPicker } from "@/components/kb-picker"
import { KbSettingsDrawer } from "@/components/kb-settings-drawer"
import type { Pending } from "@/components/stage"
import { Workspace, type Doc, type Preview } from "@/components/workspace"

const sameExceptSeparator = (a: ChunkOptions, b: ChunkOptions) =>
  (Object.keys(a) as (keyof ChunkOptions)[]).every((k) => k === "separator" || a[k] === b[k])
// 方案不用本地 OCR 参数时忽略它们；目录里查不到方案时按全部参数比较
const sameConvert = (a: ConvertOptions, b: ConvertOptions, parser?: ParserInfo) =>
  a.parser === b.parser && a.remove_header_footer === b.remove_header_footer &&
  (parser?.local_ocr_options === false ||
    (a.ocr_mode === b.ocr_mode && (a.ocr_mode === "off" || a.ocr_dpi === b.ocr_dpi)))

const fail = (title: string, e: unknown) =>
  toast({ title, description: e instanceof Error ? e.message : String(e), status: "error" })

interface Artifact extends MarkdownFile {
  doc: Doc
  markdown: string
  convert: ConvertOptions | null
  chunk: ChunkOptions
  unsaved: boolean
  saved: boolean
  /** 已保存版本在服务器上的标识；保存修改时用于检测是否被其他人改过 */
  etag?: string
}

export interface Uploading {
  loaded: number
  total: number
}

const LAST_KB = "stratum.kb"
const REFRESH_MS = 15000

function kbFromUrl(): string | null {
  return new URLSearchParams(window.location.search).get("kb")
}

/** 地址栏带上知识库名称，方便收藏或发给同事直接打开。 */
/** 进行中或已完成、尚未保存的单个解析任务。记在浏览器里，刷新页面后接着显示进度或结果。 */
interface StoredJob {
  id: string
  path: string
  pageCount: number
  settings: ParseSettings
}

const jobKey = (kb: string) => `stratum.job.${kb}`

function rememberJob(kb: string, job: StoredJob | null) {
  try {
    if (job) localStorage.setItem(jobKey(kb), JSON.stringify(job))
    else localStorage.removeItem(jobKey(kb))
  } catch { /* 存储不可用时只是不能在刷新后接回 */ }
}

function storedJob(kb: string): StoredJob | null {
  try {
    const job = JSON.parse(localStorage.getItem(jobKey(kb)) || "null")
    return job && typeof job.id === "string" && typeof job.path === "string" && job.settings ? job : null
  } catch {
    return null
  }
}

function rememberKb(name: string | null) {
  const url = new URL(window.location.href)
  if (name) url.searchParams.set("kb", name)
  else url.searchParams.delete("kb")
  window.history.replaceState(null, "", url)
  try {
    if (name) localStorage.setItem(LAST_KB, name)
    else localStorage.removeItem(LAST_KB)
  } catch { /* 存储不可用时只是不记住 */ }
}

const baseName = (path: string) => path.split("/").pop() ?? path

export default function App() {
  const [project, setProject] = useState<Project | null>(null)
  // 启动时直接进入地址栏或上次打开的知识库
  const [initialKb] = useState(() => {
    try {
      return kbFromUrl() ?? localStorage.getItem(LAST_KB)
    } catch {
      return kbFromUrl()
    }
  })
  const [booting, setBooting] = useState(!!initialKb)
  const [convertOpts, setConvertOpts] = useState<ConvertOptions>(DEFAULT_CONVERT)
  const [draft, setDraft] = useState<ChunkOptions>(DEFAULT_CHUNK)
  const catalog = useParsers()
  const [pending, setPending] = useState<Pending | null>(null)
  const [artifacts, setArtifacts] = useState<Artifact[]>([])
  const [activeArtifact, setActiveArtifact] = useState<string | null>(null)
  const current = artifacts.find((artifact) => artifact.path === activeArtifact)
  const doc = current?.doc ?? null
  const markdown = current?.markdown ?? ""
  const usedConvert = current?.convert ?? null
  const applied = current?.chunk ?? draft
  const unsaved = artifacts.some((artifact) => artifact.unsaved)
  const [busy, setBusy] = useState(false)
  const [uploading, setUploading] = useState<Uploading | null>(null)
  const [saveState, setSaveState] = useState<MorphButtonState>("idle")
  const [dragging, setDragging] = useState(false)
  const [preview, setPreview] = useState<Preview | null>(null)
  // seq 每次打开递增，用作 key：关闭时保留内容播放收起动画，再次打开时重置抽屉内状态
  const [drawer, setDrawer] = useState<{
    path: string; view: FileDrawerView; reparse: boolean; open: boolean; seq: number
    /** 批量解析：确认配置后提交这些文件 */
    batch?: { paths: string[]; skipConverted: boolean }
  } | null>(null)
  const [batchOpen, setBatchOpen] = useState(false)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [settingsSeq, setSettingsSeq] = useState(0)
  const [importing, setImporting] = useState(false)
  const [latestBatch, setLatestBatch] = useState<string | null>(null)
  const pollRef = useRef<number | null>(null)
  const runRef = useRef(0) // 每次转换或离开知识库递增，过期的轮询自行停止
  const overwriteOk = useRef<string | null>(null) // 已确认可保存修改的产物路径

  const updateCurrent = useCallback((changes: Partial<Artifact>) => {
    setArtifacts((items) => items.map((item) => item.path === activeArtifact ? { ...item, ...changes } : item))
  }, [activeArtifact])

  const showArtifact = useCallback((artifact: Artifact) => {
    setArtifacts((items) => [...items.filter((item) => item.path !== artifact.path), artifact])
    setActiveArtifact(artifact.path)
    setSaveState("idle")
    setPreview(null)
    setBatchOpen(false)
  }, [])

  // 每次生成独立版本。旧版本的文本、编辑和配置均留在会话中。
  const publishResult = useCallback(async (
    source: Omit<Doc, "mdPath">,
    text: string,
    settings: ParseSettings,
  ) => {
    if (!project) return
    const scanned = await rescan(project).catch(() => project)
    const output = nextMarkdownFile(source.path, [...scanned.artifacts, ...artifacts])
    setProject(scanned)
    showArtifact({
      ...output, doc: { ...source, mdPath: output.path }, markdown: text,
      convert: settings.convert, chunk: settings.chunk, unsaved: true, saved: false,
    })
  }, [project, artifacts, showArtifact])

  // ---------------------------------------------------------------- 知识库

  const openProject = useCallback(async (name: string) => {
    const [next, settings] = await Promise.all([openKb(name), getSettings(name)])
    setConvertOpts({ ...DEFAULT_CONVERT, ...settings.convert })
    setDraft({ ...DEFAULT_CHUNK, ...settings.chunk })
    setProject(next)
    rememberKb(next.name)
  }, [])

  useEffect(() => {
    if (!initialKb) return
    openProject(initialKb)
      .catch(() => rememberKb(null))
      .finally(() => setBooting(false))
  }, [initialKb, openProject])

  // 其他人可能同时在上传或保存：定时刷新文件列表，切回页面时立即刷新
  useEffect(() => {
    if (!project) return
    let stopped = false
    const refresh = async () => {
      if (document.visibilityState !== "visible") return
      try {
        const next = await rescan(project)
        if (!stopped) {
          setProject((p) => p && p.name === next.name && JSON.stringify(p) !== JSON.stringify(next) ? next : p)
        }
      } catch { /* 偶发网络错误等下一轮 */ }
    }
    const timer = window.setInterval(refresh, REFRESH_MS)
    const onVisible = () => void refresh()
    document.addEventListener("visibilitychange", onVisible)
    return () => {
      stopped = true
      window.clearInterval(timer)
      document.removeEventListener("visibilitychange", onVisible)
    }
  }, [project])


  // ---------------------------------------------------------------- 转换

  /** 跟踪单个解析任务直到出结果。新开始的任务和刷新页面后接回的任务（resumed）共用。 */
  const followJob = useCallback((job: StoredJob, run: number, resumed = false) => {
    if (!project) return
    const kb = project.name
    const size = project.files.find((f) => f.path === job.path)?.size ?? 0
    const base = { name: baseName(job.path), size, path: job.path }
    setPreview(null)
    setBatchOpen(false)
    setPending({ ...base, done: 0, total: job.pageCount })
    const tick = async () => {
      try {
        const state = await getJob(job.id)
        if (run !== runRef.current) return
        if (state.status === "done" && state.result) {
          // 后端可以瞬间完成；让进度组件补完各阶段和逐页计数，再切换到结果。
          await new Promise<void>((resolve) => {
            setPending({ ...base, done: job.pageCount, total: job.pageCount, onComplete: resolve })
          })
          if (run !== runRef.current) return
          await publishResult({ jobId: job.id, path: job.path, result: state.result }, state.result.markdown, job.settings)
          setPending(null)
          if (resumed) toast({ title: `已恢复「${baseName(job.path)}」的解析结果，尚未保存`, status: "info" })
          state.result.warnings.forEach((w) => toast({ title: w, status: "info", duration: 8000 }))
          return
        }
        if (state.status === "error") throw new Error(state.error || "转换失败")
        setPending({ ...base, done: state.progress.done, total: state.progress.total })
        pollRef.current = window.setTimeout(tick, 700)
      } catch (e) {
        if (run !== runRef.current) return
        rememberJob(kb, null)
        setPending(null)
        // 接回时任务已过期（服务重启或超过 3 小时）就不再提示
        if (!(resumed && e instanceof Error && e.message.includes("不存在"))) fail("转换失败", e)
      }
    }
    void tick()
  }, [project, publishResult])

  const convert = useCallback(
    async (path: string, settings: ParseSettings = { convert: convertOpts, chunk: draft }) => {
      if (!project) return
      const run = ++runRef.current
      const opts = { ...settings.convert, ...settings.chunk }
      const size = project.files.find((f) => f.path === path)?.size ?? 0
      setPreview(null)
      setBatchOpen(false)
      setPending({ name: baseName(path), size, path, done: 0, total: 0 })
      try {
        const { id, page_count } = await startKbJob(project.name, path, opts)
        if (run !== runRef.current) return
        const job = { id, path, pageCount: page_count, settings }
        rememberJob(project.name, job)
        followJob(job, run)
      } catch (e) {
        if (run !== runRef.current) return
        fail("提交解析失败", e)
        setPending(null)
      }
    },
    [project, convertOpts, draft, followJob]
  )

  // 刷新页面或重新进入知识库后，接回本浏览器尚未保存的单个解析任务
  const resumedFor = useRef<string | null>(null)
  useEffect(() => {
    if (!project || resumedFor.current === project.name) return
    resumedFor.current = project.name
    const job = storedJob(project.name)
    if (!job || pending || doc) return
    if (!project.files.some((f) => f.path === job.path)) {
      rememberJob(project.name, null)
      return
    }
    const run = ++runRef.current
    queueMicrotask(() => followJob(job, run, true))
  }, [project, pending, doc, followJob])

  /** 上传到当前知识库；autoParse 时，只上传了一个 PDF 就直接用默认参数解析它。 */
  const upload = useCallback(async (items: UploadItem[], autoParse = false) => {
    if (!project || uploading) return
    if (!items.length) {
      toast({ title: "没有找到 PDF 文件", status: "error" })
      return
    }
    setUploading({ loaded: 0, total: items.reduce((n, it) => n + it.file.size, 0) })
    try {
      const results = await uploadFiles(project.name, items, (loaded, total) => setUploading({ loaded, total }))
      const failed = results.filter((r) => r.status === "error")
      toast({
        title: `上传完成：${summarizeUpload(results)}`,
        description: failed.slice(0, 3).map((r) => `${r.path}：${r.message}`).join("\n") || undefined,
        status: failed.length ? "error" : "success",
        duration: failed.length ? 10000 : 4000,
      })
      results.filter((r) => r.status === "renamed").forEach((r) => toast({ title: r.message ?? r.path, status: "info" }))
      setProject(await rescan(project))
      const ok = results.filter((r) => r.status !== "error")
      if (autoParse && items.length === 1 && ok.length === 1 && !pending && !busy) void convert(ok[0].path)
    } catch (e) {
      fail("上传失败", e)
    } finally {
      setUploading(null)
    }
  }, [project, uploading, pending, busy, convert])

  // ---------------------------------------------------------------- 侧栏点选：抽屉 → 解析或预览

  const openDrawer = (path: string, view: FileDrawerView, reparse = false) =>
    setDrawer((d) => ({ path, view, reparse, open: true, seq: (d?.seq ?? 0) + 1 }))
  const closeDrawer = () => setDrawer((d) => d && { ...d, open: false })

  const selectPath = (path: string) => {
    if (!project || pending || busy || saveState === "loading") return
    const batchState = project.active[path]
    if (batchState) {
      // 正在批量解析：不再提供单独解析，打开批量处理查看进度
      setBatchOpen(true)
      toast({
        title: `「${baseName(path)}」正在批量解析（${batchState === "queued" ? "排队中" : "解析中"}）`,
        description: "完成后会自动保存为新版本，可在批量处理中查看进度",
        status: "info",
      })
      return
    }
    setBatchOpen(false)
    const latest = [...project.artifacts, ...artifacts]
      .filter((artifact) => artifact.pdfPath === path)
      .sort((a, b) => b.version - a.version)[0]
    if (latest) {
      void selectArtifact(latest.path)
      return
    }
    if (path === preview?.path) return
    if (path === doc?.path) {
      setPreview(null) // 回到已打开的结果
      return
    }
    openDrawer(path, "menu")
  }

  // 抽屉里确认的配置写回知识库默认参数，并用它解析
  const parse = (path: string, settings: ParseSettings) => {
    closeDrawer()
    void convert(path, settings)
  }

  const previewPath = (path: string) => {
    if (!project) return
    closeDrawer()
    setBatchOpen(false)
    setPreview({ path, url: pdfUrl(project.name, path) })
  }

  const selectArtifact = async (path: string) => {
    if (!project || pending || busy || saveState === "loading") return
    if (artifacts.some((artifact) => artifact.path === path)) {
      setActiveArtifact(path)
      setSaveState("idle")
      setPreview(null)
      setBatchOpen(false)
      return
    }
    const output = project.artifacts.find((artifact) => artifact.path === path)
    if (!output) return
    setBusy(true)
    try {
      const { markdown: text, etag, meta } = await readMarkdown(project.name, path)
      const lines = new Set(text.split(/\r?\n/))
      // 优先用保存时记录的分隔符；手动编辑过或旧文件没有记录时按内容识别
      const separator = [meta.chunk?.separator, draft.separator, ...loadSeparators()]
        .find((s): s is string => !!s && lines.has(s)) ?? meta.chunk?.separator ?? draft.separator
      showArtifact({
        ...output,
        doc: { jobId: null, path: output.pdfPath, mdPath: path, result: null },
        markdown: text, convert: meta.convert ?? null, chunk: { ...draft, ...meta.chunk, separator },
        unsaved: false, saved: true, etag,
      })
    } catch (e) {
      fail("无法打开 Markdown", e)
    } finally {
      setBusy(false)
    }
  }

  useEffect(() => () => void (pollRef.current && clearTimeout(pollRef.current)), [])

  // 整页拖放：任意位置松开即上传（拖拽区自己处理的除外）；只拖入一个 PDF 时直接开始解析
  useEffect(() => {
    if (!project) return
    let depth = 0
    const hasFiles = (e: DragEvent) => Array.from(e.dataTransfer?.types ?? []).includes("Files")
    const enter = (e: DragEvent) => {
      if (!hasFiles(e)) return
      depth += 1
      setDragging(true)
    }
    const leave = (e: DragEvent) => {
      if (!hasFiles(e)) return
      depth = Math.max(0, depth - 1)
      if (depth === 0) setDragging(false)
    }
    const over = (e: DragEvent) => hasFiles(e) && e.preventDefault()
    const drop = (e: DragEvent) => {
      depth = 0
      setDragging(false)
      if (!hasFiles(e) || !e.dataTransfer) return
      const handled = e.defaultPrevented
      e.preventDefault()
      // 必须在事件回调内同步读取 DataTransfer，之后它会失效
      if (!handled) void itemsFromDrop(e.dataTransfer).then((items) => upload(items, true))
    }
    window.addEventListener("dragenter", enter)
    window.addEventListener("dragleave", leave)
    window.addEventListener("dragover", over)
    window.addEventListener("drop", drop)
    return () => {
      window.removeEventListener("dragenter", enter)
      window.removeEventListener("dragleave", leave)
      window.removeEventListener("dragover", over)
      window.removeEventListener("drop", drop)
    }
  }, [project, upload])

  // ---------------------------------------------------------------- 分块参数

  // 确认配置后生成新版本；仅改分隔符时沿用当前版本的手动编辑。
  const reparse = async (settings: ParseSettings) => {
    if (!current || !doc || pending || busy) return
    const parser = catalog.parsers.find((p) => p.id === settings.convert.parser)
    const reuseParsed = !!doc.jobId && !!usedConvert && sameConvert(settings.convert, usedConvert, parser)
    const separatorOnly = reuseParsed && sameExceptSeparator(settings.chunk, applied) && settings.chunk.separator !== applied.separator
    closeDrawer()

    if (!reuseParsed) {
      void convert(doc.path, settings)
      return
    }
    setBusy(true)
    try {
      if (separatorOnly) {
        const text = markdown.split(applied.separator).join(settings.chunk.separator)
        await publishResult(doc, text, settings)
      } else if (doc.jobId) {
        const result = await rechunk(doc.jobId, settings.chunk)
        await publishResult({ ...doc, result }, result.markdown, settings)
      }
    } catch (e) {
      fail("重新分块失败", e)
    } finally {
      setBusy(false)
    }
  }

  // ---------------------------------------------------------------- 批量

  const startBatch = async (paths: string[], skipConverted: boolean, settings: ParseSettings) => {
    if (!project) return
    closeDrawer()
    try {
      const batch = await createBatch(project.name, paths, settings, skipConverted)
      setLatestBatch(batch.id)
      const queued = batch.counts.queued
      toast({
        title: queued ? `已提交 ${queued} 个文件，开始批量解析` : "所选文件都已转换，没有需要解析的文件",
        description: batch.counts.skipped ? `跳过 ${batch.counts.skipped} 个已转换的文件` : undefined,
        status: queued ? "success" : "info",
      })
      setProject(await rescan(project).catch(() => project))
    } catch (e) {
      fail("提交批量解析失败", e)
    }
  }

  const refreshQuietly = useCallback(() => {
    if (project) void rescan(project).then(setProject, () => undefined)
  }, [project])

  // ---------------------------------------------------------------- FastGPT

  const openSettings = () => {
    setSettingsSeq((n) => n + 1)
    setSettingsOpen(true)
  }

  /** 导入一个已保存的版本；遇到不是本系统导入的同名集合时询问替换还是保留。afterSave：紧接着保存之后导入 */
  const importVersion = async (proj: Project, pdfPath: string, mdPath: string, afterSave = false) => {
    const dataset = proj.fastgpt.dataset?.name
    if (!dataset) return
    setImporting(true)
    try {
      const body = { pdf_path: pdfPath, md_path: mdPath }
      let result = await importToFastgpt(proj.name, body)
      if (result.status === "conflict") {
        if (window.confirm(`${result.message}。\n\n确定：删除它，用这个版本替换\n取消：再选择是否保留两份`)) {
          result = await importToFastgpt(proj.name, { ...body, on_conflict: "replace" })
        } else if (window.confirm("保留原有集合，并新增一份？")) {
          result = await importToFastgpt(proj.name, { ...body, on_conflict: "keep" })
        } else {
          return
        }
      }
      if (result.status === "imported") {
        toast({
          title: `${afterSave ? "已保存并导入" : "已导入"}「${dataset}」${result.chunks != null ? `，共 ${result.chunks} 块` : ""}`,
          description: result.message ?? (result.replaced ? "已替换旧版本的集合" : undefined),
          status: result.message ? "info" : "success",
        })
      } else if (result.status === "unchanged") {
        if (!afterSave) toast({ title: "这个版本已导入，内容没有变化", status: "info" })
      } else {
        toast({ title: afterSave ? "已保存，但导入 FastGPT 失败" : "导入 FastGPT 失败", description: result.message ?? undefined, status: "error" })
      }
      setProject(await rescan(proj).catch(() => proj))
    } catch (e) {
      fail(afterSave ? "已保存，但导入 FastGPT 失败" : "导入 FastGPT 失败", e)
    } finally {
      setImporting(false)
    }
  }
  // save 用 useCallback 缓存，通过 ref 调用最新的 importVersion
  const importVersionRef = useRef(importVersion)
  useEffect(() => {
    importVersionRef.current = importVersion
  })

  // ---------------------------------------------------------------- 编辑与导出

  const onMarkdown = (md: string) => {
    updateCurrent({ markdown: md, unsaved: true })
  }

  const download = () => {
    if (!doc) return
    const blob = new Blob([markdown], { type: "text/markdown;charset=utf-8" })
    const a = document.createElement("a")
    a.href = URL.createObjectURL(blob)
    a.download = baseName(doc.mdPath)
    a.click()
    setTimeout(() => URL.revokeObjectURL(a.href), 1000)
  }

  /** 保存当前版本。andImport：保存后导入 FastGPT（知识库开启了自动导入时总是导入） */
  const save = useCallback(async (andImport?: boolean) => {
    // 只拦截保存中；刚保存完（按钮还显示「已保存」）时仍可再次保存或保存并导入
    if (!project || !current || pending || preview || busy || saveState === "loading") return
    if (current.saved && overwriteOk.current !== current.path) {
      if (!window.confirm(`确定保存对 ${current.path} 的修改？`)) return
    }
    setSaveState("loading")
    try {
      let savedPath = current.path
      if (current.saved) {
        const { etag } = await overwriteMarkdown(project.name, current.path, current.markdown, current.etag)
        updateCurrent({ unsaved: false, etag })
        overwriteOk.current = current.path
      } else {
        const saved = await saveVersion(project.name, {
          pdf_path: current.pdfPath, markdown: current.markdown, version: current.version,
          job_id: current.doc.jobId, convert: current.convert, chunk: current.chunk,
        })
        // 预定的版本号可能已被其他人占用，服务端会顺延；同步改名，并让占用了新路径的未保存版本重新编号
        setArtifacts((items) => {
          const renamed = items.map((item) => item.path !== current.path ? item : {
            ...item, path: saved.path, version: saved.version, etag: saved.etag, unsaved: false, saved: true,
            doc: { ...item.doc, mdPath: saved.path },
          })
          return renamed.map((item) => {
            if (item.saved || item.path !== saved.path) return item
            const next = nextMarkdownFile(item.pdfPath, [...project.artifacts, ...renamed, { path: saved.path, pdfPath: saved.pdf_path, version: saved.version }])
            return { ...item, ...next, doc: { ...item.doc, mdPath: next.path } }
          })
        })
        setActiveArtifact(saved.path)
        overwriteOk.current = saved.path
        savedPath = saved.path
        if (current.doc.jobId && storedJob(project.name)?.id === current.doc.jobId) rememberJob(project.name, null)
        if (saved.path !== current.path) {
          toast({ title: `${baseName(current.path)} 已被占用，保存为 ${baseName(saved.path)}`, status: "info" })
        }
      }
      const fresh = await rescan(project).catch(() => project)
      setProject(fresh)
      setSaveState("success")
      if (fresh.fastgpt.configured && fresh.fastgpt.dataset && (andImport === true || fresh.fastgpt.auto_import)) {
        void importVersionRef.current(fresh, current.pdfPath, savedPath, true)
      }
    } catch (e) {
      fail("保存失败", e)
      setSaveState("error")
    }
  }, [project, current, pending, preview, busy, saveState, updateCurrent])

  /** 导入当前打开的版本；未保存（或有未保存的修改）时先保存再导入 */
  const importCurrent = async () => {
    if (!project || !current || importing) return
    if (!project.fastgpt.configured) {
      toast({ title: "服务端未配置 FastGPT，无法导入", description: "请联系管理员配置 STRATUM_FASTGPT_URL 和 STRATUM_FASTGPT_API_KEY", status: "error" })
      return
    }
    if (!project.fastgpt.dataset) {
      toast({ title: "请先在知识库设置中绑定 FastGPT 知识库", status: "info" })
      openSettings()
      return
    }
    if (!current.saved || current.unsaved) {
      await save(true)
      return
    }
    await importVersion(project, current.pdfPath, current.path)
  }

  const importStatus = (() => {
    if (!project?.fastgpt.configured || !current) return null
    const saved = current.saved && !current.unsaved
    const button = (
      <Hint label={saved ? "把这个版本导入绑定的 FastGPT 知识库" : "保存到知识库，并导入绑定的 FastGPT 知识库"}>
        <Button
          aria-label="导入 FastGPT"
          className="rounded-full"
          disabled={importing || busy || saveState === "loading"}
          onClick={importCurrent}
          size="xs"
          variant="outline"
        >
          {importing ? <LoaderCircle className="animate-spin" /> : <Send />}
          {importing ? "导入中" : saved ? "导入 FastGPT" : "保存并导入 FastGPT"}
        </Button>
      </Hint>
    )
    if (!project.fastgpt.dataset) return button
    const record = project.fastgpt.imports[current.pdfPath]
    let text = "未导入 FastGPT"
    let tone = "text-muted-foreground"
    if (record?.error) {
      text = "FastGPT 导入失败"
      tone = "text-red-600 dark:text-red-400"
    } else if (record?.md_path === current.path) {
      // 用文件标识判断：导入后这个版本又被修改过（或有未保存的编辑）时需要重新导入
      const changed = current.unsaved || (!!record.md_etag && record.md_etag !== current.etag)
      text = changed ? "已修改，需重新导入" : `已导入 FastGPT${record.chunks != null ? ` · ${record.chunks} 块` : ""}`
      tone = changed ? "text-amber-600 dark:text-amber-400" : "text-emerald-600 dark:text-emerald-400"
    } else if (record) {
      text = `FastGPT 中为 ${baseName(record.md_path)}`
      tone = "text-amber-600 dark:text-amber-400"
    }
    return (
      <span className="flex items-center gap-2">
        <span className={`whitespace-nowrap text-xs ${tone}`} title={record?.error ?? undefined}>{text}</span>
        {button}
      </span>
    )
  })()

  // 保存按钮的成功 / 失败状态停留片刻后恢复
  useEffect(() => {
    if (saveState !== "success" && saveState !== "error") return
    const t = window.setTimeout(() => setSaveState("idle"), 1800)
    return () => clearTimeout(t)
  }, [saveState])

  useEffect(() => {
    if (!project) return
    const handler = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
        e.preventDefault()
        save()
      }
    }
    window.addEventListener("keydown", handler)
    return () => window.removeEventListener("keydown", handler)
  }, [project, save])

  useEffect(() => {
    if (!unsaved && !uploading) return
    const handler = (e: BeforeUnloadEvent) => e.preventDefault()
    window.addEventListener("beforeunload", handler)
    return () => window.removeEventListener("beforeunload", handler)
  }, [unsaved, uploading])

  // ---------------------------------------------------------------- 切换知识库

  /** name 为 null 时回到知识库列表。用户取消确认时当前知识库和结果保持不变。 */
  const switchProject = async (name: string | null) => {
    if (busy || saveState === "loading") return
    if (uploading && !window.confirm("正在上传，确定离开当前知识库？")) return
    if (unsaved && !window.confirm("当前知识库中有未保存的版本，确定离开？")) return
    if (pending && !window.confirm("正在解析，确定离开当前知识库？")) return
    try {
      runRef.current += 1
      if (pollRef.current) clearTimeout(pollRef.current)
      setPending(null)
      setArtifacts([])
      setActiveArtifact(null)
      setSaveState("idle")
      overwriteOk.current = null
      setPreview(null)
      setDrawer(null)
      setBatchOpen(false)
      setLatestBatch(null)
      setSettingsOpen(false)
      if (name) await openProject(name)
      else {
        setProject(null)
        rememberKb(null)
      }
    } catch (e) {
      fail("无法打开知识库", e)
    }
  }

  const refresh = async () => {
    if (!project) return
    try {
      setProject(await rescan(project))
    } catch (e) {
      fail("刷新失败", e)
    }
  }

  if (booting) return <div className="h-svh bg-muted/30" />

  return (
    <TooltipProvider delay={350}>
      {project ? (
        <Workspace
          activePath={drawer?.open && !drawer.batch ? drawer.path : pending ? pending.path : batchOpen ? null : (preview?.path ?? doc?.path ?? null)}
          batch={batchOpen ? (
            <BatchStage
              latestBatch={latestBatch}
              onClose={() => setBatchOpen(false)}
              onOpen={(mdPath) => void selectArtifact(mdPath)}
              onProgress={refreshQuietly}
              onSettings={openSettings}
              onStart={(paths, skipConverted) =>
                setDrawer((d) => ({ path: "", view: "config", reparse: false, open: true, seq: (d?.seq ?? 0) + 1, batch: { paths, skipConverted } }))}
              project={project}
            />
          ) : null}
          onSettings={openSettings}
          importStatus={importStatus}
          onBatch={() => {
            if (pending || busy) return
            setPreview(null)
            setBatchOpen((v) => !v)
          }}
          busy={busy}
          doc={doc}
          draft={applied}
          markdown={markdown}
          onDownload={download}
          onUpload={(items) => upload(items)}
          onFiles={(items) => upload(items, true)}
          onSwitchProject={switchProject}
          onClosePreview={() => setPreview(null)}
          onParsePreview={() => preview && openDrawer(preview.path, "config")}
          onMarkdown={onMarkdown}
          onReparse={() => doc && openDrawer(doc.path, "config", true)}
          onSelectArtifact={selectArtifact}
          artifacts={artifacts.map(({ path, pdfPath, version, unsaved }) => ({ path, pdfPath, version, unsaved }))}
          onRefresh={refresh}
          onSave={save}
          onSelectPath={selectPath}
          pending={pending}
          preview={preview}
          project={project}
          saveState={saveState}
          separator={applied.separator}
          uploading={uploading}
        />
      ) : (
        <KbPicker onOpen={(name) => openProject(name)} />
      )}
      {dragging && project && (doc || pending || preview) && (
        <div className="pointer-events-none fixed inset-0 z-50 flex items-center justify-center bg-background/70 backdrop-blur-sm">
          <div className="flex flex-col items-center gap-3 rounded-3xl border-2 border-foreground/20 border-dashed px-16 py-12 text-muted-foreground">
            <Upload className="size-8" strokeWidth={1.5} />
            <span className="text-sm">松开即上传到「{project.name}」</span>
          </div>
        </div>
      )}
      {project && drawer && (
        <FileDrawer
          catalog={catalog}
          initialView={drawer.view}
          key={drawer.seq}
          onClose={closeDrawer}
          confirmLabel={drawer.batch ? `开始批量解析（${drawer.batch.paths.length}）` : undefined}
          onParse={(settings) => drawer.batch
            ? startBatch(drawer.batch.paths, drawer.batch.skipConverted, settings)
            : drawer.reparse ? reparse(settings) : parse(drawer.path, settings)}
          onPreview={() => previewPath(drawer.path)}
          open={drawer.open}
          path={drawer.path}
          reparse={drawer.reparse}
          settings={drawer.reparse ? { convert: usedConvert ?? convertOpts, chunk: applied } : { convert: convertOpts, chunk: draft }}
        />
      )}
      {project && (
        <KbSettingsDrawer
          catalog={catalog}
          key={`${project.name}:${settingsSeq}`}
          onClose={() => setSettingsOpen(false)}
          onSaved={(settings, fastgptState) => {
            setConvertOpts(settings.convert)
            setDraft(settings.chunk)
            setProject((p) => p && { ...p, fastgpt: fastgptState })
            void refreshQuietly()
          }}
          open={settingsOpen}
          project={project}
          settings={{ convert: convertOpts, chunk: draft }}
        />
      )}
      <Toaster />
    </TooltipProvider>
  )
}
