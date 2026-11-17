import { useState } from "react"
import { LoaderCircle, Trash2 } from "lucide-react"

import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"

interface DeleteConfirmProps {
  /** 要删除的 PDF 数量 */
  count: number
  /** 其中已导入 FastGPT 的数量 */
  imported: number
  /** 绑定的 FastGPT 知识库名称 */
  dataset?: string
  onConfirm: (withFastgpt: boolean) => Promise<void>
  onCancel: () => void
  className?: string
}

/** 删除确认：PDF 与全部 Markdown 版本一定删除；已导入的文件可选择是否同时删除 FastGPT 中的集合。 */
export function DeleteConfirm({ count, imported, dataset, onConfirm, onCancel, className }: DeleteConfirmProps) {
  const [withFastgpt, setWithFastgpt] = useState(true)
  const [busy, setBusy] = useState(false)

  const confirm = async () => {
    setBusy(true)
    try {
      await onConfirm(imported > 0 && withFastgpt)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className={cn("flex flex-col gap-3 rounded-xl border border-red-200 bg-red-50/60 p-3 text-sm dark:border-red-900/50 dark:bg-red-950/20", className)} role="alertdialog" aria-label="确认删除">
      <p>
        删除 {count} 个 PDF 及其全部 Markdown 版本，删除后无法恢复。
      </p>
      {imported > 0 && (
        <label className="flex items-start gap-2 text-xs">
          <input checked={withFastgpt} className="mt-0.5 accent-red-500" disabled={busy} onChange={(e) => setWithFastgpt(e.target.checked)} type="checkbox" />
          <span>
            同时删除 FastGPT{dataset ? `「${dataset}」` : ""}中对应的 {imported} 个集合
            <span className="block text-muted-foreground">不勾选时 FastGPT 中的集合保留，问答仍可引用</span>
          </span>
        </label>
      )}
      <div className="flex justify-end gap-2">
        <Button disabled={busy} onClick={onCancel} size="sm" variant="ghost">取消</Button>
        <Button className="bg-red-500 text-white hover:bg-red-600" disabled={busy} onClick={confirm} size="sm">
          {busy ? <LoaderCircle className="animate-spin" /> : <Trash2 />}
          确认删除
        </Button>
      </div>
    </div>
  )
}
