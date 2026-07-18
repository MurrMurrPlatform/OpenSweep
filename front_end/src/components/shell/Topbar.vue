<script setup lang="ts">
import { useRoute, useRouter } from 'vue-router'
import { Sun, Moon, LogOut, Volume2, VolumeX, User, Search as SearchIcon } from 'lucide-vue-next'
import { openCommandPalette } from '@/composables/useCommandPalette'
import { computed } from 'vue'
import { useTheme } from '@/composables/useTheme'
import { soundsEnabled, toggleSounds } from '@/lib/notifySound'
import ColorSchemeSwitcher from '@/components/shell/ColorSchemeSwitcher.vue'
import NotificationBell from '@/components/shell/NotificationBell.vue'
import WorkspaceSwitcher from '@/components/shell/WorkspaceSwitcher.vue'
import { Avatar, AvatarFallback } from '@/components/ui/avatar'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Separator } from '@/components/ui/separator'
import { SidebarTrigger } from '@/components/ui/sidebar'
import { useCurrentUserStore } from '@/stores/currentUserStore'
import { signOut } from '@/services/auth'

const route = useRoute()
const router = useRouter()
const { theme, toggle } = useTheme()
const currentUser = useCurrentUserStore()

const title = computed(() => route.meta.title ?? 'OpenSweep')
const eyebrow = computed(() => route.meta.eyebrow ?? '')

const initials = computed(() =>
  currentUser.displayName
    .split(/\s+/)
    .map(part => part[0])
    .filter(Boolean)
    .slice(0, 2)
    .join('')
    .toUpperCase() || '?',
)
</script>

<template>
  <header class="sticky top-0 z-30 flex h-14 shrink-0 items-center gap-2 border-b bg-background/95 px-3 backdrop-blur supports-[backdrop-filter]:bg-background/80 sm:px-4">
    <SidebarTrigger class="-ml-1" />
    <Separator orientation="vertical" class="h-5" />
    <WorkspaceSwitcher />
    <div class="hidden min-w-0 items-center gap-3 md:flex">
      <Separator orientation="vertical" class="h-5" />
      <div class="min-w-0">
        <div v-if="eyebrow" class="text-[10px] uppercase tracking-wider text-muted-foreground">{{ eyebrow }}</div>
        <h1 class="truncate text-sm font-semibold leading-tight">{{ title }}</h1>
      </div>
    </div>

    <div class="ml-auto flex items-center gap-1">
      <button
        type="button"
        class="mr-1 hidden h-8 items-center gap-2 rounded-md border bg-muted/40 px-2.5 text-sm text-muted-foreground transition-colors hover:bg-accent hover:text-foreground md:inline-flex"
        @click="openCommandPalette()"
      >
        <SearchIcon class="size-3.5" />
        <span class="pr-4">Search…</span>
        <kbd class="pointer-events-none rounded border bg-background px-1.5 font-mono text-[10px] font-medium text-muted-foreground">⌘K</kbd>
      </button>
      <Button
        variant="ghost"
        size="icon-sm"
        class="md:hidden"
        title="Search"
        @click="openCommandPalette()"
      >
        <SearchIcon class="size-4" />
      </Button>
      <NotificationBell />
      <ColorSchemeSwitcher class="hidden sm:inline-flex" />
      <Button
        variant="ghost"
        size="icon-sm"
        class="hidden sm:inline-flex"
        :title="theme === 'dark' ? 'Switch to light' : 'Switch to dark'"
        @click="toggle()"
      >
        <Sun v-if="theme === 'dark'" class="size-4" />
        <Moon v-else class="size-4" />
      </Button>
      <Button
        variant="ghost"
        size="icon-sm"
        class="hidden sm:inline-flex"
        :title="soundsEnabled ? 'Mute notification sounds' : 'Unmute notification sounds'"
        @click="toggleSounds()"
      >
        <Volume2 v-if="soundsEnabled" class="size-4" />
        <VolumeX v-else class="size-4" />
      </Button>

      <DropdownMenu>
        <DropdownMenuTrigger as-child>
          <Button variant="ghost" size="icon-sm" class="rounded-full" title="Account">
            <Avatar class="size-7">
              <AvatarFallback class="text-xs font-medium">{{ initials }}</AvatarFallback>
            </Avatar>
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" class="w-52">
          <DropdownMenuLabel class="truncate">{{ currentUser.displayName }}</DropdownMenuLabel>
          <DropdownMenuSeparator />
          <DropdownMenuItem class="gap-2" @select="router.push('/settings/account')">
            <User class="size-4" /> Account settings
          </DropdownMenuItem>
          <!-- Theme/sound/scheme controls fold into the menu on phones. -->
          <DropdownMenuItem class="gap-2 sm:hidden" @select="toggle()">
            <Sun v-if="theme === 'dark'" class="size-4" />
            <Moon v-else class="size-4" />
            {{ theme === 'dark' ? 'Light mode' : 'Dark mode' }}
          </DropdownMenuItem>
          <DropdownMenuItem class="gap-2 sm:hidden" @select="toggleSounds()">
            <Volume2 v-if="soundsEnabled" class="size-4" />
            <VolumeX v-else class="size-4" />
            {{ soundsEnabled ? 'Mute sounds' : 'Unmute sounds' }}
          </DropdownMenuItem>
          <DropdownMenuSeparator />
          <DropdownMenuItem class="gap-2" @select="signOut()">
            <LogOut class="size-4" /> Sign out
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  </header>
</template>
