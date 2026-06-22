-- BaT Scanner — two-player backend schema (Phase 5).
-- DRAFT / UNVERIFIED: this encodes the eng-review + Codex-hardened design, but it has NOT
-- been run against a live project. Before trusting it, paste it into your Supabase SQL editor
-- and run tests/test_rls_smoke.py against the project (the CRITICAL go-live gate from the
-- eng review). RLS is the ENTIRE security boundary — the anon key is public.
--
-- Provisioning order (your steps, ~15 min):
--   1. Create a free Supabase project. ROTATE the service_role key that was pasted in chat.
--   2. Auth > Providers: enable Email (magic link + OTP). Auth > disable new signups.
--   3. Pre-create the TWO users (Auth > Users > invite) — you + your dad.
--   4. Edit the two emails in allowed_email() below, then run this whole file.
--   5. Run the anon smoke test. Only after it passes is the boundary trustworthy.
--   6. service_role key -> GitHub Actions secret SUPABASE_SERVICE_ROLE (never in the frontend).

-- ---------------------------------------------------------------------------
-- Allowlist: the coarse outer gate. The REAL boundary is the per-row predicates below.
-- ---------------------------------------------------------------------------
create or replace function public.allowed_email() returns boolean
language sql stable as $$
  select coalesce(auth.jwt() ->> 'email', '') in (
    'you@example.com',      -- TODO: your email
    'dad@example.com'       -- TODO: your dad's email
  );
$$;

-- ---------------------------------------------------------------------------
-- Tables
-- ---------------------------------------------------------------------------
create table if not exists public.profiles (
  id uuid primary key references auth.users(id) on delete cascade,
  display_name text,
  last_seen_at timestamptz default now()
);

-- denormalized car copy. WRITTEN ONLY by the daily Action (service_role, bypasses RLS).
-- Client reads it for context; the frontend mostly joins bat_id against the static snapshot.
create table if not exists public.listing_cache (
  bat_id        bigint primary key,
  title         text,
  year          int,
  thumbnail_url text,          -- URL only, never a downloaded image
  ends_at       timestamptz,
  bid_amount    int,
  bid_currency  text,
  deal_pct      numeric,
  is_deal       boolean,
  miles         int,
  updated_at    timestamptz default now()
);

create table if not exists public.stars (        -- personal favorite (private to its owner)
  user_id uuid not null references auth.users(id) on delete cascade,
  bat_id  bigint not null,
  created_at timestamptz default now(),
  primary key (user_id, bat_id)
);

create table if not exists public.watchlist (    -- SHARED list both can see
  bat_id   bigint primary key,
  added_by uuid not null references auth.users(id) on delete cascade,
  note     text,
  created_at timestamptz default now()
);

create table if not exists public.sends (        -- "Dad sent you a car"
  id        bigint generated always as identity primary key,
  from_user uuid not null references auth.users(id) on delete cascade,
  to_user   uuid not null references auth.users(id) on delete cascade,
  bat_id    bigint not null,
  note      text,
  seen_at   timestamptz,
  created_at timestamptz default now()
);

create table if not exists public.reactions (    -- shared emoji signal
  user_id uuid not null references auth.users(id) on delete cascade,
  bat_id  bigint not null,
  emoji   text not null,
  created_at timestamptz default now(),
  primary key (user_id, bat_id, emoji)
);

create table if not exists public.saved_filters ( -- personal "categories" = saved filter specs
  id        bigint generated always as identity primary key,
  user_id   uuid not null references auth.users(id) on delete cascade,
  name      text not null,
  spec      jsonb not null,
  created_at timestamptz default now()
);

-- ---------------------------------------------------------------------------
-- RLS — enabled on EVERY table, default-deny. Per-row predicates are the real boundary:
-- an allowlist-only SELECT would leak each user's private rows to the other (Codex catch).
-- Every write policy has an explicit WITH CHECK so neither user can write as the other.
-- ---------------------------------------------------------------------------
alter table public.profiles      enable row level security;
alter table public.listing_cache enable row level security;
alter table public.stars         enable row level security;
alter table public.watchlist     enable row level security;
alter table public.sends         enable row level security;
alter table public.reactions     enable row level security;
alter table public.saved_filters enable row level security;

-- profiles: both allowlisted users can read names; you edit only your own row.
create policy profiles_sel on public.profiles for select using (allowed_email());
create policy profiles_ins on public.profiles for insert with check (id = auth.uid());
create policy profiles_upd on public.profiles for update using (id = auth.uid()) with check (id = auth.uid());

-- listing_cache: allowlisted read; NO client write (service_role writes it, bypassing RLS).
create policy cache_sel on public.listing_cache for select using (allowed_email());

-- stars: PRIVATE to the owner (your stars are yours, not Dad's to see).
create policy stars_sel on public.stars for select using (user_id = auth.uid());
create policy stars_ins on public.stars for insert with check (user_id = auth.uid());
create policy stars_del on public.stars for delete using (user_id = auth.uid());

-- watchlist: SHARED — both allowlisted users see it; you only write/delete your own rows.
create policy watch_sel on public.watchlist for select using (allowed_email());
create policy watch_ins on public.watchlist for insert with check (added_by = auth.uid());
create policy watch_del on public.watchlist for delete using (added_by = auth.uid());

-- sends: visible only to sender + recipient; you send AS yourself; recipient marks seen.
create policy sends_sel on public.sends for select using (from_user = auth.uid() or to_user = auth.uid());
create policy sends_ins on public.sends for insert with check (from_user = auth.uid() and allowed_email());
create policy sends_upd on public.sends for update using (to_user = auth.uid()) with check (to_user = auth.uid());

-- reactions: shared signal; you write only your own.
create policy react_sel on public.reactions for select using (allowed_email());
create policy react_ins on public.reactions for insert with check (user_id = auth.uid());
create policy react_del on public.reactions for delete using (user_id = auth.uid());

-- saved_filters: PRIVATE to the owner (sharing a filter rides the sends rail later).
create policy filters_sel on public.saved_filters for select using (user_id = auth.uid());
create policy filters_ins on public.saved_filters for insert with check (user_id = auth.uid());
create policy filters_del on public.saved_filters for delete using (user_id = auth.uid());

-- ---------------------------------------------------------------------------
-- Auth bootstrap: create a profiles row on first login so the UI never wedges (Codex catch).
-- ---------------------------------------------------------------------------
create or replace function public.handle_new_user() returns trigger
language plpgsql security definer set search_path = public as $$
begin
  insert into public.profiles (id, display_name)
  values (new.id, split_part(new.email, '@', 1))
  on conflict (id) do nothing;
  return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function public.handle_new_user();

-- ---------------------------------------------------------------------------
-- Realtime: enable only on the social tables (NOT prices). Confirm in the smoke test that
-- anon cannot subscribe. Presence channels are authorized to the authed role in the client.
-- ---------------------------------------------------------------------------
alter publication supabase_realtime add table public.stars;
alter publication supabase_realtime add table public.watchlist;
alter publication supabase_realtime add table public.sends;
alter publication supabase_realtime add table public.reactions;
