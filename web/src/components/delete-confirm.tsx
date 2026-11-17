import { useState } from "react"
import { LoaderCircle, Trash2, X } from "lucide-react"
import { Drawer } from "vaul"

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
        {count === 1 ? "删除这个 PDF" : `删除 ${count} 个 PDF`}及其全部 Markdown 版本，删除后无法恢复。
      </p>
      {imported > 0 && (
        <label className="flex items-start gap-2 text-xs">
          <input checked={withFastgpt} className="mt-0.5 accent-red-500" disabled={busy} onChange={(e) => setWithFastgpt(e.target.checked)} type="checkbox" />
          <span>
            同时删除 FastGPT{dataset ? `「${dataset}」` : ""}中对应的{count === 1 ? "集合" : ` ${imported} 个集合`}
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


interface DeleteFileDrawerProps {
  path: string
  open: boolean
  imported: boolean
  dataset?: string
  onConfirm: (withFastgpt: boolean) => Promise<void>
  onClose: () => void
}

/** 从左侧列表删除单个 PDF 时的确认抽屉。 */
export function DeleteFileDrawer({ path, open, imported, dataset, onConfirm, onClose }: DeleteFileDrawerProps) {
  const name = path.split("/").pop() ?? path
  return (
    <Drawer.Root onOpenChange={(v) => !v && onClose()} open={open}>
      <Drawer.Portal>
        <Drawer.Overlay className="fixed inset-0 z-40 bg-black/40" />
        <Drawer.Content
          aria-describedby={undefined}
          className="fixed inset-x-4 bottom-4 z-50 mx-auto max-w-[400px] overflow-hidden rounded-[36px] bg-white p-6 outline-hidden dark:bg-neutral-900"
        >
          <div className="flex items-center justify-between gap-3">
            <Drawer.Title className="min-w-0 truncate text-lg font-medium text-neutral-900 dark:text-neutral-100" title={path}>
              删除「{name}」
            </Drawer.Title>
            <Button
              aria-label="关闭"
              className="shrink-0 rounded-full hover:bg-neutral-100 dark:hover:bg-neutral-800"
              onClick={onClose}
              size="icon"
              variant="secondary"
            >
              <X className="text-neutral-600 dark:text-neutral-400" size={18} />
            </Button>
          </div>
          <DeleteConfirm
            className="mt-5"
            count={1}
            dataset={dataset}
            imported={imported ? 1 : 0}
            onCancel={onClose}
            onConfirm={onConfirm}
          />
        </Drawer.Content>
      </Drawer.Portal>
    </Drawer.Root>
  )
}
