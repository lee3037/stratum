import { useEffect, useState } from "react"
import { LoaderCircle, RefreshCw, X } from "lucide-react"
import { Drawer } from "vaul"

import type { FastgptDataset, FastgptState, KbSettings, Project } from "@/lib/library"
import { bindDataset, listDatasets, putSettings } from "@/lib/library"
import type { ParserCatalog } from "@/lib/use-parsers"
import { Button } from "@/components/ui/button"
import { SettingsPanel } from "@/components/settings-panel"
import { toast } from "@/components/toaster"

interface KbSettingsDrawerProps {
  open: boolean
  project: Project
  settings: KbSettings
  catalog: ParserCatalog
  onClose: () => void
  onSaved: (settings: KbSettings, fastgpt: FastgptState) => void
}

const fail = (title: string, e: unknown) =>
  toast({ title, description: e instanceof Error ? e.message : String(e), status: "error" })

/** 知识库设置：绑定 FastGPT 知识库，修改默认解析与分块参数。只有在这里保存才会改变知识库默认值。 */
export function KbSettingsDrawer({ open, project, settings, catalog, onClose, onSaved }: KbSettingsDrawerProps) {
  const [draft, setDraft] = useState(settings)
  const [datasetId, setDatasetId] = useState(project.fastgpt.dataset?.id ?? "")
  const [datasets, setDatasets] = useState<FastgptDataset[] | null>(null)
  const [datasetError, setDatasetError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const { configured, dataset } = project.fastgpt

  const fetchDatasets = () => listDatasets().then(setDatasets, (e) => {
    setDatasets([])
    setDatasetError(e instanceof Error ? e.message : String(e))
  })
  const loadDatasets = () => {
    setDatasets(null)
    setDatasetError(null)
    void fetchDatasets()
  }
  // 打开时读取一次 FastGPT 知识库列表（组件每次打开都会重新挂载）
  useEffect(() => {
    if (configured) void fetchDatasets()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [configured])

  const { chunk } = draft
  const valid = !!chunk.separator.trim() && Number.isInteger(chunk.max_chars) && chunk.max_chars >= 100 &&
    chunk.max_chars <= 10000 && Number.isInteger(chunk.min_chars) && chunk.min_chars >= 0 && chunk.min_chars <= chunk.max_chars
  const settingsChanged = JSON.stringify(draft) !== JSON.stringify(settings)
  const bindingChanged = datasetId !== (dataset?.id ?? "")
  const imported = Object.keys(project.fastgpt.imports).length

  const save = async () => {
    if (saving) return
    if (bindingChanged && dataset && imported &&
      !window.confirm(`更换绑定后，原知识库「${dataset.name}」中已导入的 ${imported} 个文件不会被删除，也不再显示导入状态。确定更换？`)) return
    setSaving(true)
    try {
      const saved = settingsChanged ? await putSettings(project.name, draft) : settings
      const fastgpt = bindingChanged ? await bindDataset(project.name, datasetId || null) : project.fastgpt
      onSaved({ convert: saved.convert, chunk: saved.chunk }, fastgpt)
      toast({ title: "知识库设置已保存", status: "success" })
      onClose()
    } catch (e) {
      fail("保存失败", e)
    } finally {
      setSaving(false)
    }
  }

  return (
    <Drawer.Root handleOnly onOpenChange={(v) => !v && onClose()} open={open}>
      <Drawer.Portal>
        <Drawer.Overlay className="fixed inset-0 z-40 bg-black/40" />
        <Drawer.Content
          aria-describedby={undefined}
          className="fixed inset-x-4 bottom-4 z-50 mx-auto max-w-[440px] overflow-hidden rounded-[36px] bg-white p-4 outline-hidden dark:bg-neutral-900"
        >
          <div className="flex items-center justify-between gap-3 px-1 pt-1 pb-4">
            <Drawer.Title className="min-w-0 truncate text-lg font-medium" title={project.name}>
              知识库设置 · {project.name}
            </Drawer.Title>
            <Button aria-label="关闭" className="shrink-0 rounded-full" onClick={onClose} size="icon" variant="secondary">
              <X size={18} />
            </Button>
          </div>
          <div className="flex max-h-[min(66svh,600px)] flex-col gap-3 overflow-y-auto overscroll-contain px-[3px] py-1 [scrollbar-width:thin]">
            <section aria-label="FastGPT 绑定" className="flex flex-col gap-2 rounded-[18px] border border-border p-4">
              <h3 className="text-[13px] font-semibold">FastGPT 知识库</h3>
              {!configured ? (
                <p className="text-xs text-muted-foreground">
                  服务端未配置 FastGPT（STRATUM_FASTGPT_URL、STRATUM_FASTGPT_API_KEY），暂时无法导入。请联系管理员。
                </p>
              ) : (
                <>
                  <div className="flex items-center gap-2">
                    <select
                      aria-label="绑定的 FastGPT 知识库"
                      className="h-9 min-w-0 flex-1 rounded-lg border border-border bg-background px-2 text-sm"
                      disabled={datasets === null}
                      onChange={(e) => setDatasetId(e.target.value)}
                      value={datasetId}
                    >
                      <option value="">不绑定</option>
                      {dataset && !datasets?.some((d) => d.id === dataset.id) && (
                        <option value={dataset.id}>{dataset.name}</option>
                      )}
                      {datasets?.map((d) => (
                        <option key={d.id} value={d.id}>
                          {d.name}{d.vector_model ? `（${d.vector_model}）` : ""}
                        </option>
                      ))}
                    </select>
                    <Button aria-label="重新读取 FastGPT 知识库列表" onClick={loadDatasets} size="icon-sm" variant="ghost">
                      {datasets === null ? <LoaderCircle className="animate-spin" /> : <RefreshCw />}
                    </Button>
                  </div>
                  {datasetError && <p className="text-xs text-red-600 dark:text-red-400">{datasetError}</p>}
                  <p className="text-[11px] text-muted-foreground">
                    导入时，FastGPT 按自定义分隔符「{draft.chunk.separator}」分块，与本系统的分块一一对应；
                    每个 PDF 在 FastGPT 中对应一个集合，导入新版本时自动替换旧集合。
                  </p>
                </>
              )}
            </section>
            <p className="px-1 pt-1 text-[13px] font-semibold">默认解析参数</p>
            <SettingsPanel
              catalog={catalog}
              convert={draft.convert}
              onChange={(c) => setDraft((d) => ({ ...d, chunk: c }))}
              onConvert={(c) => setDraft((d) => ({ ...d, convert: c }))}
              value={draft.chunk}
            />
            <p className="px-1 text-[11px] text-muted-foreground">
              单个解析和批量解析默认使用这组参数；解析时临时调整的参数只对那一次有效。修改后，已保存和已导入的结果不受影响。
            </p>
          </div>
          <div className="mt-4 flex items-center gap-2.5 px-[3px]">
            <Button className="h-10 w-[35%] rounded-3xl" onClick={onClose} variant="secondary">取消</Button>
            <Button
              className="h-10 min-w-0 flex-1 rounded-3xl bg-sky-500 text-white hover:bg-sky-600"
              disabled={!valid || saving || (!settingsChanged && !bindingChanged)}
              onClick={save}
            >
              {saving && <LoaderCircle className="animate-spin" />}
              保存设置
            </Button>
          </div>
        </Drawer.Content>
      </Drawer.Portal>
    </Drawer.Root>
  )
}
