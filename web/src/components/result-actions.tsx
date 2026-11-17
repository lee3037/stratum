import { useEffect, useState, type ReactNode } from "react"
import { Check, Copy, Download, LoaderCircle, RotateCcw, Save, X } from "lucide-react"

import type { MorphButtonState } from "@/components/spectrumui/morph-button"
import { copyToClipboardWithMeta } from "@/components/ui/copy-button"
import { Button } from "@/components/ui/button"
import { Hint, Keys } from "@/components/hint"
import { toast } from "@/components/toaster"

interface ResultActionsProps {
  markdown: string
  busy: boolean
  saveState: MorphButtonState
  onDownload: () => void
  onSave: () => void
  onReparse: () => void
}

const SAVE_LABELS = { idle: "保存到知识库", loading: "保存中", success: "已保存", error: "保存失败" }
const SAVE_ICONS = { idle: Save, loading: LoaderCircle, success: Check, error: X }

/** 结果工具栏：一排图标按钮，鼠标停留时显示说明，点击直接执行。 */
export function ResultActions({ markdown, busy, saveState, onDownload, onSave, onReparse }: ResultActionsProps) {
  const [copied, setCopied] = useState(false)
  useEffect(() => {
    if (!copied) return
    const timer = window.setTimeout(() => setCopied(false), 2000)
    return () => clearTimeout(timer)
  }, [copied])

  const copy = async () => {
    if (await copyToClipboardWithMeta(markdown)) setCopied(true)
    else toast({ title: "复制失败", status: "error" })
  }
  const SaveIcon = SAVE_ICONS[saveState]

  return (
    <div aria-label="Markdown 产物操作" className="flex h-9 items-center gap-0.5 rounded-full border border-border bg-background p-0.5 shadow-xs" role="group">
      <Action disabled={busy} icon={copied ? <Check /> : <Copy />} label={copied ? "已复制" : "复制 Markdown"} onClick={copy} />
      <Action disabled={busy} icon={<Download />} label="下载 .md" onClick={onDownload} />
      <Action
        disabled={busy || saveState !== "idle"}
        hint={saveState === "idle" ? <>保存到知识库 <Keys>Ctrl / ⌘ S</Keys></> : undefined}
        icon={<SaveIcon className={saveState === "loading" ? "animate-spin" : undefined} />}
        label={SAVE_LABELS[saveState]}
        onClick={onSave}
      />
      <Action
        disabled={busy || saveState === "loading"}
        icon={<RotateCcw className={busy ? "animate-spin" : undefined} />}
        label={busy ? "处理中" : "重新解析分块"}
        onClick={onReparse}
      />
      <span aria-live="polite" className="sr-only">{copied ? "已复制" : saveState !== "idle" ? SAVE_LABELS[saveState] : ""}</span>
    </div>
  )
}

function Action({ label, hint, icon, disabled, onClick }: {
  label: string
  hint?: ReactNode
  icon: ReactNode
  disabled?: boolean
  onClick: () => void
}) {
  return (
    <Hint label={hint ?? label}>
      <Button
        aria-label={label}
        className="rounded-full text-muted-foreground hover:text-foreground [&_svg]:size-3.5"
        disabled={disabled}
        onClick={onClick}
        size="icon-sm"
        variant="ghost"
      >
        {icon}
      </Button>
    </Hint>
  )
}
