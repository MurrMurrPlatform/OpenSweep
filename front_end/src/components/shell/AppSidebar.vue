<script setup lang="ts">
import { RouterLink, useRoute } from 'vue-router'
import OpenSweepLogo from '@/components/branding/OpenSweepLogo.vue'
import SweepMark from '@/components/branding/SweepMark.vue'
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarRail,
  useSidebar,
} from '@/components/ui/sidebar'
import { useUiStore } from '@/stores/uiStore'
import { type NavItem, useNavSections } from '@/composables/useNavSections'

const route = useRoute()
const ui = useUiStore()
const { isMobile, setOpenMobile } = useSidebar()
const { sections } = useNavSections()

function isActive(item: NavItem): boolean {
  if (item.matchNames?.includes(String(route.name))) return true
  if (item.exact || item.to === '/') return route.path === item.to
  return route.path === item.to || route.path.startsWith(item.to + '/')
}

function isDisabled(item: NavItem): boolean {
  return Boolean(item.scoped) && !ui.currentRepoSlug
}

// Tapping a nav link on mobile should dismiss the sheet.
function onNavigate() {
  if (isMobile.value) setOpenMobile(false)
}
</script>

<template>
  <Sidebar collapsible="icon" variant="inset">
    <SidebarHeader>
      <RouterLink
        to="/"
        class="flex h-12 items-center overflow-hidden px-2 group-data-[collapsible=icon]:justify-center group-data-[collapsible=icon]:px-0"
        @click="onNavigate"
      >
        <OpenSweepLogo class="h-8 shrink-0 group-data-[collapsible=icon]:hidden" />
        <span class="hidden h-8 w-8 place-items-center rounded-md bg-primary text-primary-foreground group-data-[collapsible=icon]:grid">
          <SweepMark class="h-5 w-5" aria-hidden="true" />
        </span>
      </RouterLink>
    </SidebarHeader>

    <SidebarContent>
      <SidebarGroup v-for="(section, idx) in sections" :key="idx">
        <SidebarGroupLabel v-if="section.label">{{ section.label }}</SidebarGroupLabel>
        <SidebarGroupContent>
          <SidebarMenu>
            <SidebarMenuItem v-for="item in section.items" :key="item.to + item.label">
              <SidebarMenuButton
                v-if="isDisabled(item)"
                disabled
                :tooltip="item.label"
                title="Select a workspace"
              >
                <component :is="item.icon" />
                <span>{{ item.label }}</span>
              </SidebarMenuButton>
              <SidebarMenuButton
                v-else
                as-child
                :is-active="isActive(item)"
                :tooltip="item.label"
              >
                <RouterLink :to="item.to" @click="onNavigate">
                  <component :is="item.icon" />
                  <span>{{ item.label }}</span>
                </RouterLink>
              </SidebarMenuButton>
            </SidebarMenuItem>
          </SidebarMenu>
        </SidebarGroupContent>
      </SidebarGroup>
    </SidebarContent>

    <SidebarFooter>
      <div class="px-2 py-1 text-xs text-sidebar-foreground/60 group-data-[collapsible=icon]:hidden">
        <div>v0.3.0 · local</div>
      </div>
    </SidebarFooter>
    <SidebarRail />
  </Sidebar>
</template>
