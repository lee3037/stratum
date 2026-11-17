import { useMemo, useRef, useState } from "react"
import { FilePlus2, FolderUp, ListChecks, RefreshCw, Search, X } from "lucide-react"

import type { Project, UploadItem } from "@/lib/library"
import { itemsFromInput } from "@/lib/library"
import { Button } from "@/components/ui/button"
import { Hint } from "@/components/hint"
import { PdfIcon } from "@/components/file-type-icon"
import { TreeNav } from "@/components/spectrumui/tree-nav"

interface ProjectNavProps {
  project: Project
  active: string | null
  /** 转换中：禁止切换文件 */
  locked: boolean
  uploading: { loaded: number; total: number } | null
  onSelect: (path: string) => void
  onRefresh: () => void
  /** 上传到当前知识库（不自动解析） */
  onUpload: (items: UploadItem[]) => void
  /** 打开批量处理视图 */
  onBatch: () => void
  batchOpen: boolean
}

/** 左栏上半部分：知识库中的全部 PDF。 */
export function ProjectNav({ project, active, locked, uploading, onSelect, onRefresh, onUpload, onBatch, batchOpen }: ProjectNavProps) {
  const filesRef = useRef<HTMLInputElement>(null)
  const [query, setQuery] = useState("")
  const q = query.trim().toLowerCase()
  const pending = useMemo(() => new Set(project.fastgpt.pending), [project.fastgpt.pending])
  const folderRef = useRef<HTMLInputElement>(null)
  const items = useMemo(
    () =>
      project.files.filter((f) => !q || f.path.toLowerCase().includes(q)).map((f) => ({
        label: f.path,
        href: `#${encodeURIComponent(f.path)}`,
        icon: <PdfIcon />,
        badge: project.active[f.path] === "running" ? "解析中"
          : project.active[f.path] === "queued" ? "排队中"
            : !f.converted ? undefined
              : !project.fastgpt.dataset ? "已转换"
                : project.fastgpt.imports[f.path] && !pending.has(f.path) ? "已导入" : "待导入",
      })),
    [project.files, project.active, project.fastgpt, pending, q]
  )
  const converted = project.files.filter((f) => f.converted).length
  const percent = uploading && uploading.total ? Math.round((uploading.loaded / uploading.total) * 100) : 0

  const pick = (list: FileList | null) => {
    const picked = list ? itemsFromInput(list) : []
    if (picked.length) onUpload(picked)
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex items-center gap-0.5 py-3 pr-2 pl-5">
        <span className="text-xs font-medium text-muted-foreground tabular-nums">
          {uploading ? `上传中 ${percent}%` : (
            <>
              PDF {project.files.length}
              {converted > 0 && ` · 已转换 ${converted}`}
            </>
          )}
        </span>
        <Hint label="批量解析、打包下载">
          <Button
            aria-label="批量处理"
            aria-pressed={batchOpen}
            className="ml-auto"
            disabled={locked}
            onClick={onBatch}
            size="icon-sm"
            variant={batchOpen ? "secondary" : "ghost"}
          >
            <ListChecks />
          </Button>
        </Hint>
        <Hint label="上传 PDF（可多选）">
          <Button
            aria-label="上传 PDF"
            disabled={!!uploading}
            onClick={() => filesRef.current?.click()}
            size="icon-sm"
            variant="ghost"
          >
            <FilePlus2 />
          </Button>
        </Hint>
        <Hint label="上传文件夹（保留子目录结构）">
          <Button aria-label="上传文件夹" disabled={!!uploading} onClick={() => folderRef.current?.click()} size="icon-sm" variant="ghost">
            <FolderUp />
          </Button>
        </Hint>
        <input
          accept="application/pdf,.pdf"
          className="hidden"
          multiple
          onChange={(e) => {
            pick(e.target.files)
            e.target.value = ""
          }}
          ref={filesRef}
          type="file"
        />
        <input
          className="hidden"
          multiple
          onChange={(e) => {
            pick(e.target.files)
            e.target.value = ""
          }}
          ref={(el) => {
            folderRef.current = el
            el?.setAttribute("webkitdirectory", "")
          }}
          type="file"
        />
        <Hint label="刷新文件列表">
          <Button aria-label="刷新文件列表" onClick={onRefresh} size="icon-sm" variant="ghost">
            <RefreshCw />
          </Button>
        </Hint>
      </div>
      {(project.files.length > 8 || query) && (
        <label className="mx-4 mb-2 flex h-8 items-center gap-2 rounded-lg border border-border bg-background px-2.5 text-sm focus-within:ring-2 focus-within:ring-ring">
          <Search className="size-4 shrink-0 text-muted-foreground" />
          <input
            aria-label="搜索 PDF"
            className="min-w-0 flex-1 bg-transparent outline-none"
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => e.key === "Escape" && setQuery("")}
            placeholder="搜索文件名或文件夹"
            value={query}
          />
          {query && (
            <button aria-label="清除搜索" className="text-muted-foreground hover:text-foreground" onClick={() => setQuery("")} type="button">
              <X className="size-3.5" />
            </button>
          )}
        </label>
      )}
      {uploading && (
        <div className="mx-5 mb-2 h-1 overflow-hidden rounded-full bg-muted">
          <div className="h-full bg-sky-500 transition-[width]" style={{ width: `${percent}%` }} />
        </div>
      )}
      <div className="min-h-0 flex-1 overflow-y-auto pr-3 pb-4 pl-2">
        {items.length ? (
          <TreeNav
            activeHref={active ? `#${encodeURIComponent(active)}` : undefined}
            className={locked ? "pointer-events-none opacity-60" : undefined}
            items={items}
            onSelect={(item, e) => {
              e.preventDefault()
              onSelect(item.label)
            }}
          />
        ) : (
          <p className="px-3 py-2 text-sm text-muted-foreground">
            {project.files.length ? "没有符合条件的 PDF" : "知识库中还没有 PDF，点击右上角上传"}
          </p>
        )}
      </div>
    </div>
  )
}
