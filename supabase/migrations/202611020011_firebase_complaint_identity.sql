alter table public.complaints
  drop constraint if exists complaints_anonymous_identity_check;

alter table public.complaints
  add constraint complaints_anonymous_identity_check check (
    is_anonymous
    or reporter_user_id is not null
    or reporter_firebase_uid is not null
  );
