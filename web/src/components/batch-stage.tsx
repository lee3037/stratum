import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { ChevronDown, ChevronRight, Download, Layers, LoaderCircle, RotateCcw, Search, Send, Square, Trash2, X } from "lucide-react"

import type { Batch, BatchItem, ConflictPolicy, ExportMode, ImportJob, Project } from "@/lib/library"
import { cancelBatch, downloadZip, getBatch, getImportJob, listBatches, retryBatch, startImportJob } from "@/lib/library"
import { cn } from "@/lib/utils"
import { Badge } from "@/components/ui/badge"
import { DeleteConfirm } from "@/components/delete-confirm"
import { Button } from "@/components/ui/button"
import { Hint } from "@/components/hint"
import { toast } from "@/components/toaster"

type Filter = "all" | "pending" | "converted"

interface BatchStageProps {
  project: Project
  /** 选好文件后进入解析配置；确认后由调用方提交批次 */
  onStart: (paths: string[], skipConverted: boolean) => void
  /** 批次进度变化：刷新文件列表 */
  onProgress: () => void
  /** 打开某个已保存的版本 */
  onOpen: (mdPath: string) => void
  /** 打开知识库设置（绑定 FastGPT） */
  onSettings: () => void
  onClose: () => void
  /** 新提交的批次编号；变化时立即刷新并展开 */
  latestBatch: string | null
  /** 删除所选 PDF（及其全部版本）；withFastgpt 时同时删除 FastGPT 集合。返回已删除的路径 */
  onDelete: (paths: string[], withFastgpt: boolean) => Promise<string[]>
}

const fail = (title: string, e: unknown) =>
  toast({ title, description: e instanceof Error ? e.message : String(e), status: "error" })

const STATUS: Record<BatchItem["status"], { label: string; className: string }> = {
  queued: { label: "排队中", className: "text-muted-foreground" },
  running: { label: "解析中", className: "text-sky-600 dark:text-sky-400" },
  done: { label: "完成", className: "text-emerald-600 dark:text-emerald-400" },
  error: { label: "失败", className: "text-red-600 dark:text-red-400" },
  skipped: { label: "跳过", className: "text-muted-foreground" },
  cancelled: { label: "已取消", className: "text-muted-foreground" },
}

const MODES: { id: ExportMode; label: string; hint: string }[] = [
  { id: "fastgpt", label: "FastGPT 导入格式", hint: "每个 PDF 只取最新版本，文件名去掉 _vN" },
  { id: "latest", label: "最新版本", hint: "每个 PDF 只取最新版本，保留版本号" },
  { id: "all", label: "全部版本", hint: "包含每个 PDF 的所有版本" },
]

const formatBytes = (n: number) =>
  n >= 1024 * 1024 ? `${(n / 1024 / 1024).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`

const formatTime = (t: number) => new Date(t * 1000).toLocaleString("zh-CN", { hour12: false })

/** 主区域的批量处理视图：勾选文件 → 批量解析或打包下载；下方显示最近的批次进度。 */
export function BatchStage({ project, onStart, onProgress, onOpen, onSettings, onClose, latestBatch, onDelete }: BatchStageProps) {
  const [query, setQuery] = useState("")
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [filter, setFilter] = useState<Filter>("all")
  const [selected, setSelected] = useState<Set<string>>(() => new Set())
  const [skipConverted, setSkipConverted] = useState(true)
  const [mode, setMode] = useState<ExportMode>("fastgpt")
  const [withPdf, setWithPdf] = useState(false)
  const [downloading, setDownloading] = useState<string | null>(null)
  const [conflict, setConflict] = useState<ConflictPolicy>("skip")
  const [importJob, setImportJob] = useState<ImportJob | null>(null)
  const { dataset, imports } = project.fastgpt
  const stale = useMemo(() => new Set(project.fastgpt.pending), [project.fastgpt.pending])

  // 导入任务进行中时每秒刷新，结束后刷新文件列表并汇总
  useEffect(() => {
    if (!importJob || importJob.status !== "running") return
    const timer = window.setTimeout(async () => {
      try {
        const next = await getImportJob(project.name, importJob.id)
        setImportJob(next)
        if (next.status === "done") {
          onProgress()
          const c = next.counts
          toast({
            title: `导入完成：成功 ${c.imported ?? 0} 个${c.unchanged ? `，无变化 ${c.unchanged} 个` : ""}`,
            description: [c.skipped && `跳过 ${c.skipped} 个（同名集合）`, c.error && `失败 ${c.error} 个`].filter(Boolean).join("，") || undefined,
            status: c.error ? "error" : "success",
          })
        }
      } catch (e) {
        fail("读取导入进度失败", e)
        setImportJob(null)
      }
    }, 1000)
    return () => window.clearTimeout(timer)
  }, [importJob, project.name, onProgress])

  const startImport = async () => {
    if (!dataset) {
      onSettings()
      return
    }
    try {
      const job = await startImportJob(project.name, chosen, conflict)
      if (!job.total) toast({ title: "没有需要导入的文件", status: "info" })
      setImportJob(job.total ? job : null)
    } catch (e) {
      fail("提交导入失败", e)
    }
  }

  const latestVersion = useMemo(() => {
    const map = new Map<string, number>()
    for (const a of project.artifacts) map.set(a.pdfPath, Math.max(map.get(a.pdfPath) ?? 0, a.version))
    return map
  }, [project.artifacts])

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase()
    return project.files.filter((f) =>
      (!q || f.path.toLowerCase().includes(q)) &&
      (filter === "all" || (filter === "pending" ? !f.converted : f.converted)))
  }, [project.files, query, filter])

  // 文件被移走后不再保留选择
  const chosen = useMemo(() => project.files.filter((f) => selected.has(f.path)).map((f) => f.path), [project.files, selected])
  const allVisibleSelected = visible.length > 0 && visible.every((f) => selected.has(f.path))
  const toggle = (path: string) => setSelected((s) => {
    const next = new Set(s)
    if (next.has(path)) next.delete(path)
    else next.add(path)
    return next
  })
  const toggleVisible = () => setSelected((s) => {
    const next = new Set(s)
    for (const f of visible) {
      if (allVisibleSelected) next.delete(f.path)
      else next.add(f.path)
    }
    return next
  })

  const download = async (key: string, body: Parameters<typeof downloadZip>[1]) => {
    if (downloading) return
    setDownloading(key)
    try {
      const info = await downloadZip(project.name, body)
      toast({ title: `已打包 ${info.markdown} 个 Markdown${info.pdf ? `、${info.pdf} 个 PDF` : ""}`, status: "success" })
    } catch (e) {
      fail("打包下载失败", e)
    } finally {
      setDownloading(null)
    }
  }

  return (
    <div className="h-full overflow-y-auto px-6 py-5">
      <div className="mx-auto flex max-w-4xl flex-col gap-6">
        <div className="flex items-center gap-2">
          <h2 className="text-lg font-semibold">批量处理</h2>
          <span className="text-sm text-muted-foreground">批量解析在服务器上进行，每完成一个文件自动保存为新版本，关闭页面也不影响。</span>
          <Hint label="关闭批量处理">
            <Button aria-label="关闭批量处理" className="ml-auto" onClick={onClose} size="icon-sm" variant="ghost">
              <X />
            </Button>
          </Hint>
        </div>

        <section aria-label="选择文件" className="flex flex-col gap-3 rounded-2xl border border-border bg-card p-4">
          <div className="flex flex-wrap items-center gap-2">
            <label className="flex h-8 min-w-48 flex-1 items-center gap-2 rounded-lg border border-border bg-background px-2.5 text-sm focus-within:ring-2 focus-within:ring-ring">
              <Search className="size-4 text-muted-foreground" />
              <input
                aria-label="按路径筛选"
                className="min-w-0 flex-1 bg-transparent outline-none"
                onChange={(e) => setQuery(e.target.value)}
                placeholder="按文件名或文件夹筛选"
                value={query}
              />
            </label>
            <div className="flex rounded-lg bg-muted p-0.5 text-xs" role="radiogroup" aria-label="解析状态">
              {([["all", "全部"], ["pending", "未转换"], ["converted", "已转换"]] as const).map(([id, label]) => (
                <button
                  aria-checked={filter === id}
                  className={cn("rounded-md px-2.5 py-1", filter === id ? "bg-background shadow-xs" : "text-muted-foreground")}
                  key={id}
                  onClick={() => setFilter(id)}
                  role="radio"
                  type="button"
                >
                  {label}
                </button>
              ))}
            </div>
          </div>

          <div className="max-h-[42svh] overflow-y-auto rounded-xl border border-border">
            <table className="w-full text-sm">
              <thead className="sticky top-0 bg-muted/80 text-xs text-muted-foreground backdrop-blur">
                <tr>
                  <th className="w-10 px-3 py-2 text-left">
                    <input
                      aria-label="全选当前列表"
                      checked={allVisibleSelected}
                      className="accent-sky-500"
                      disabled={!visible.length}
                      onChange={toggleVisible}
                      type="checkbox"
                    />
                  </th>
                  <th className="py-2 text-left font-medium">文件（{visible.length}）</th>
                  <th className="w-28 py-2 text-left font-medium">状态</th>
                  {dataset && <th className="w-28 py-2 text-left font-medium">FastGPT</th>}
                  <th className="w-20 py-2 pr-3 text-right font-medium">大小</th>
                </tr>
              </thead>
              <tbody>
                {visible.map((f) => {
                  const active = project.active[f.path]
                  const version = latestVersion.get(f.path)
                  return (
                    <tr className="border-border/60 border-t hover:bg-muted/40" key={f.path}>
                      <td className="px-3 py-1.5">
                        <input
                          aria-label={`选择 ${f.path}`}
                          checked={selected.has(f.path)}
                          className="accent-sky-500"
                          onChange={() => toggle(f.path)}
                          type="checkbox"
                        />
                      </td>
                      <td className="max-w-0 truncate py-1.5" title={f.path}>
                        <label className="cursor-pointer" onClick={() => toggle(f.path)}>{f.path}</label>
                      </td>
                      <td className="py-1.5 text-xs">
                        {active ? (
                          <span className={STATUS[active].className}>{STATUS[active].label}</span>
                        ) : version ? (
                          <span className="text-emerald-600 dark:text-emerald-400">已转换 v{version}</span>
                        ) : (
                          <span className="text-muted-foreground">未转换</span>
                        )}
                      </td>
                      {dataset && (
                        <td className="py-1.5 text-xs">
                          {!version ? null : imports[f.path]?.error && stale.has(f.path) ? (
                            <span className="text-red-600 dark:text-red-400" title={imports[f.path].error ?? undefined}>导入失败</span>
                          ) : imports[f.path] && !stale.has(f.path) ? (
                            <span className="text-emerald-600 dark:text-emerald-400">已导入 v{imports[f.path].version}</span>
                          ) : (
                            <span className="text-amber-600 dark:text-amber-400">{imports[f.path] ? "有更新" : "待导入"}</span>
                          )}
                        </td>
                      )}
                      <td className="py-1.5 pr-3 text-right text-xs text-muted-foreground tabular-nums">{formatBytes(f.size)}</td>
                    </tr>
                  )
                })}
                {!visible.length && (
                  <tr>
                    <td className="px-3 py-6 text-center text-sm text-muted-foreground" colSpan={dataset ? 5 : 4}>没有符合条件的 PDF</td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>

          <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
            <span className="text-sm tabular-nums">已选 {chosen.length} 个</span>
            {chosen.length > 0 && (
              <button className="text-xs text-muted-foreground underline-offset-2 hover:underline" onClick={() => setSelected(new Set())} type="button">
                清除选择
              </button>
            )}
            <label className="flex items-center gap-1.5 text-xs">
              <input checked={skipConverted} className="accent-sky-500" onChange={(e) => setSkipConverted(e.target.checked)} type="checkbox" />
              跳过已转换的文件
            </label>
            <Button
              className="ml-auto text-red-600 hover:bg-red-50 hover:text-red-700 dark:text-red-400 dark:hover:bg-red-950/30"
              disabled={!chosen.length || confirmDelete}
              onClick={() => setConfirmDelete(true)}
              variant="ghost"
            >
              <Trash2 />
              删除所选（{chosen.length}）
            </Button>
            <Button
              className="bg-sky-500 text-white hover:bg-sky-600"
              disabled={!chosen.length}
              onClick={() => onStart(chosen, skipConverted)}
            >
              <Layers />
              批量解析（{chosen.length}）
            </Button>
          </div>
          {confirmDelete && chosen.length > 0 && (
            <DeleteConfirm
              count={chosen.length}
              dataset={dataset?.name}
              imported={dataset ? chosen.filter((p) => imports[p]).length : 0}
              onCancel={() => setConfirmDelete(false)}
              onConfirm={async (withFastgpt) => {
                const deleted = new Set(await onDelete(chosen, withFastgpt))
                setSelected((s) => new Set([...s].filter((p) => !deleted.has(p))))
                setConfirmDelete(false)
              }}
            />
          )}

          <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-border border-t pt-3">
            <span className="text-sm font-medium">打包下载</span>
            <select
              aria-label="下载内容"
              className="h-8 rounded-lg border border-border bg-background px-2 text-sm"
              onChange={(e) => setMode(e.target.value as ExportMode)}
              title={MODES.find((m) => m.id === mode)?.hint}
              value={mode}
            >
              {MODES.map((m) => <option key={m.id} value={m.id}>{m.label}</option>)}
            </select>
            <label className="flex items-center gap-1.5 text-xs">
              <input checked={withPdf} className="accent-sky-500" onChange={(e) => setWithPdf(e.target.checked)} type="checkbox" />
              包含 PDF 原文
            </label>
            <span className="text-xs text-muted-foreground">{MODES.find((m) => m.id === mode)?.hint}；附汇总.csv</span>
            <Button
              className="ml-auto"
              disabled={!!downloading || !project.artifacts.length}
              onClick={() => download("selection", { mode, with_pdf: withPdf, paths: chosen })}
              variant="outline"
            >
              {downloading === "selection" ? <LoaderCircle className="animate-spin" /> : <Download />}
              {chosen.length ? `下载所选（${chosen.length}）` : "下载整个知识库"}
            </Button>
          </div>
          <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-border border-t pt-3">
            <span className="text-sm font-medium">导入 FastGPT</span>
            {dataset ? (
              <>
                <span className="text-xs text-muted-foreground">
                  →「{dataset.name}」，每个 PDF 导入最新保存的版本
                  {project.fastgpt.auto_import && "。已开启自动导入：批量解析完成的文件会自动导入，遇到非本系统导入的同名集合时跳过"}
                </span>
                <select
                  aria-label="遇到同名集合"
                  className="h-8 rounded-lg border border-border bg-background px-2 text-sm"
                  onChange={(e) => setConflict(e.target.value as ConflictPolicy)}
                  title="FastGPT 中已有同名集合、且不是通过本系统导入时的处理方式"
                  value={conflict}
                >
                  <option value="skip">已有同名集合时跳过</option>
                  <option value="replace">已有同名集合时替换</option>
                  <option value="keep">已有同名集合时保留两份</option>
                </select>
                <Button
                  className="ml-auto"
                  disabled={importJob?.status === "running" || (!chosen.length && !stale.size)}
                  onClick={startImport}
                  variant="outline"
                >
                  {importJob?.status === "running" ? <LoaderCircle className="animate-spin" /> : <Send />}
                  {chosen.length ? `导入所选（${chosen.length}）` : `导入全部待导入（${stale.size}）`}
                </Button>
              </>
            ) : (
              <>
                <span className="text-xs text-muted-foreground">
                  {project.fastgpt.configured ? "当前知识库还没有绑定 FastGPT 知识库。" : "服务端未配置 FastGPT。"}
                </span>
                {project.fastgpt.configured && (
                  <Button className="ml-auto" onClick={onSettings} variant="outline">去绑定</Button>
                )}
              </>
            )}
            {importJob && (
              <div className="w-full text-xs">
                <div className="flex items-center gap-2 text-muted-foreground tabular-nums">
                  {importJob.status === "running" ? "导入中" : "导入结束"}：
                  {importJob.total - (importJob.counts.queued ?? 0) - (importJob.counts.running ?? 0)} / {importJob.total}
                  <button className="ml-auto underline-offset-2 hover:underline" onClick={() => setImportJob(null)} type="button">收起</button>
                </div>
                <ul className="mt-1 max-h-40 overflow-y-auto">
                  {importJob.items.filter((i) => ["error", "skipped", "conflict"].includes(i.status) || i.message).map((i) => (
                    <li className="flex gap-2 py-0.5" key={i.pdf_path}>
                      <span className={i.status === "error" ? "text-red-600 dark:text-red-400" : "text-amber-600 dark:text-amber-400"}>
                        {i.status === "error" ? "失败" : i.status === "skipped" ? "跳过" : "提示"}
                      </span>
                      <span className="min-w-0 truncate">{i.pdf_path}</span>
                      <span className="ml-auto truncate text-muted-foreground" title={i.message ?? undefined}>{i.message}</span>
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        </section>

        <BatchList
          downloading={downloading}
          kb={project.name}
          latestBatch={latestBatch}
          onDownload={(batchId) => download(`batch:${batchId}`, { mode, with_pdf: withPdf, batch_id: batchId })}
          onOpen={onOpen}
          onProgress={onProgress}
        />
      </div>
    </div>
  )
}

interface BatchListProps {
  kb: string
  latestBatch: string | null
  downloading: string | null
  onProgress: () => void
  onOpen: (mdPath: string) => void
  onDownload: (batchId: string) => void
}

/** 最近的批次：运行中时每 2 秒刷新；完成数变化时通知刷新文件列表。 */
function BatchList({ kb, latestBatch, downloading, onProgress, onOpen, onDownload }: BatchListProps) {
  const [batches, setBatches] = useState<Batch[] | null>(null)
  const [expanded, setExpanded] = useState<string | null>(latestBatch)
  // 新提交的批次自动展开（渲染时对比，不用 effect）
  const [seenLatest, setSeenLatest] = useState(latestBatch)
  if (latestBatch !== seenLatest) {
    setSeenLatest(latestBatch)
    if (latestBatch) setExpanded(latestBatch)
  }
  const [details, setDetails] = useState<Batch | null>(null)
  const progressKey = useRef("")
  const running = !!batches?.some((b) => b.status === "running")

  const load = useCallback(async () => {
    try {
      const list = await listBatches(kb)
      setBatches(list)
      const key = list.map((b) => `${b.id}:${b.counts.done}:${b.counts.error}:${b.status}`).join("|")
      if (progressKey.current && key !== progressKey.current) onProgress()
      progressKey.current = key
      if (expanded) setDetails(await getBatch(kb, expanded))
    } catch { /* 网络抖动等下一轮 */ }
  }, [kb, expanded, onProgress])

  useEffect(() => {
    void load()
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") void load()
    }, running ? 2000 : 10000)
    return () => window.clearInterval(timer)
  }, [load, running])


  const act = async (task: () => Promise<Batch>, label: string) => {
    try {
      await task()
      await load()
    } catch (e) {
      fail(label, e)
    }
  }

  if (!batches) return null
  return (
    <section aria-label="最近的批次" className="flex flex-col gap-2">
      <h3 className="text-sm font-medium text-muted-foreground">最近的批次</h3>
      {!batches.length && <p className="text-sm text-muted-foreground">还没有批量任务。</p>}
      {batches.map((b) => {
        const finished = b.total - b.counts.queued - b.counts.running
        const open = expanded === b.id
        const retryable = b.counts.error + b.counts.cancelled
        return (
          <div className="rounded-2xl border border-border bg-card" key={b.id}>
            <div className="flex flex-wrap items-center gap-x-3 gap-y-2 px-4 py-3">
              <button
                aria-expanded={open}
                className="flex items-center gap-1.5 text-sm font-medium"
                onClick={() => setExpanded(open ? null : b.id)}
                type="button"
              >
                {open ? <ChevronDown className="size-4" /> : <ChevronRight className="size-4" />}
                {formatTime(b.created)}
              </button>
              <Badge variant={b.status === "running" ? "default" : "outline"}>
                {b.status === "running" ? "进行中" : b.status === "cancelled" ? "已取消" : "已结束"}
              </Badge>
              <span className="text-xs text-muted-foreground tabular-nums">
                {finished} / {b.total} · 完成 {b.counts.done}
                {b.counts.error > 0 && <span className="text-red-600 dark:text-red-400"> · 失败 {b.counts.error}</span>}
                {b.counts.skipped > 0 && ` · 跳过 ${b.counts.skipped}`}
                {b.counts.cancelled > 0 && ` · 取消 ${b.counts.cancelled}`}
              </span>
              <div className="ml-auto flex items-center gap-1">
                {b.status === "running" && (
                  <Button onClick={() => act(() => cancelBatch(kb, b.id), "取消失败")} size="sm" variant="ghost">
                    <Square />取消剩余
                  </Button>
                )}
                {b.status !== "running" && retryable > 0 && (
                  <Button onClick={() => act(() => retryBatch(kb, b.id), "重试失败")} size="sm" variant="ghost">
                    <RotateCcw />重试 {retryable} 个
                  </Button>
                )}
                {b.counts.done > 0 && (
                  <Button disabled={!!downloading} onClick={() => onDownload(b.id)} size="sm" variant="ghost">
                    {downloading === `batch:${b.id}` ? <LoaderCircle className="animate-spin" /> : <Download />}下载本批结果
                  </Button>
                )}
              </div>
              <div className="h-1.5 w-full overflow-hidden rounded-full bg-muted">
                <div
                  className={cn("h-full transition-[width]", b.counts.error ? "bg-amber-500" : "bg-sky-500")}
                  style={{ width: `${b.total ? (finished / b.total) * 100 : 0}%` }}
                />
              </div>
            </div>
            {open && details?.id === b.id && details.items && (
              <ul className="max-h-72 overflow-y-auto border-border border-t px-4 py-2 text-sm">
                {details.items.map((item) => (
                  <li className="flex items-baseline gap-3 py-1" key={item.path}>
                    <span className={cn("w-12 shrink-0 text-xs", STATUS[item.status].className)}>
                      {item.status === "running" && <LoaderCircle className="mr-0.5 inline size-3 animate-spin" />}
                      {STATUS[item.status].label}
                    </span>
                    {item.status === "done" && item.version_path ? (
                      <button className="min-w-0 truncate text-left hover:underline" onClick={() => onOpen(item.version_path!)} title={`打开 ${item.version_path}`} type="button">
                        {item.version_path}
                      </button>
                    ) : (
                      <span className="min-w-0 truncate" title={item.path}>{item.path}</span>
                    )}
                    <span className="ml-auto shrink-0 truncate text-xs text-muted-foreground" title={item.error ?? item.message ?? undefined}>
                      {item.status === "done"
                        ? `${item.pages ?? "?"} 页 · ${item.chunks ?? "?"} 块${item.ocr_pages ? ` · OCR ${item.ocr_pages} 页` : ""}`
                        : item.error ?? item.message ?? ""}
                    </span>
                    {item.fastgpt && <AutoImportTag result={item.fastgpt} />}
                  </li>
                ))}
              </ul>
            )}
          </div>
        )
      })}
    </section>
  )
}


/** 批次明细中自动导入 FastGPT 的结果 */
function AutoImportTag({ result }: { result: NonNullable<BatchItem["fastgpt"]> }) {
  const tone = result.status === "imported" || result.status === "unchanged"
    ? "text-emerald-600 dark:text-emerald-400"
    : result.status === "error" ? "text-red-600 dark:text-red-400" : "text-amber-600 dark:text-amber-400"
  const label = { imported: "已导入", unchanged: "已导入", skipped: "导入跳过", error: "导入失败" }[result.status as string] ?? result.status
  return <span className={cn("shrink-0 text-xs", tone)} title={result.message ?? undefined}>{label}</span>
}
