import { useState } from "react"
import { Check, ChevronsUpDown, Library, LibraryBig } from "lucide-react"

import type { KbSummary, Project } from "@/lib/library"
import { listKbs } from "@/lib/library"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"

interface ProjectSwitcherProps {
  project: Project
  /** 切换到另一个知识库；null 回到知识库列表 */
  onSwitch: (name: string | null) => void
}

/** 页头的知识库名：下拉列出全部知识库，点选即切换。 */
export function ProjectSwitcher({ project, onSwitch }: ProjectSwitcherProps) {
  const [kbs, setKbs] = useState<KbSummary[]>([])

  return (
    <DropdownMenu onOpenChange={(open) => open && listKbs().then((r) => setKbs(r.kbs), () => setKbs([]))}>
      <DropdownMenuTrigger render={<Button className="max-w-56 gap-2 px-2" variant="ghost" />}>
        <Library />
        <span className="truncate">{project.name}</span>
        <ChevronsUpDown className="text-muted-foreground" />
      </DropdownMenuTrigger>
      <DropdownMenuContent className="max-h-[60svh] w-64 overflow-y-auto">
        {kbs.map((kb) => {
          const current = kb.name === project.name
          return (
            <DropdownMenuItem key={kb.name} onClick={() => !current && onSwitch(kb.name)}>
              <Library className="text-muted-foreground" />
              <span className="min-w-0 flex-1 truncate">{kb.name}</span>
              {current ? <Check /> : <span className="text-xs text-muted-foreground tabular-nums">{kb.pdfs}</span>}
            </DropdownMenuItem>
          )
        })}
        {kbs.length > 0 && <DropdownMenuSeparator />}
        <DropdownMenuItem onClick={() => onSwitch(null)}>
          <LibraryBig className="text-muted-foreground" />
          全部知识库 / 新建…
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
