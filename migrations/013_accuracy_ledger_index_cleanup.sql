-- Cover the optional self-referencing correction-lineage foreign key.
create index if not exists scanner_signal_supersedes_idx
    on public.scanner_signal_events (supersedes_signal_id)
    where supersedes_signal_id is not null;
