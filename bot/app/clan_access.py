"""Persistent moderation workflow; first run is always preview-only."""
import asyncio
import logging
import time
import httpx
from app.access_policy import AccessPolicy, highest_role
from app.settings import settings

log = logging.getLogger(__name__)


class ClanAccess:
    def __init__(self, path, bindings):
        self.policy = AccessPolicy(path)
        self.bindings = bindings
        self.lock = asyncio.Lock()
        self.client = httpx.AsyncClient(timeout=15)
        with self.policy.connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS access_activation(group_id INTEGER PRIMARY KEY, actor INTEGER, at REAL);
                CREATE TABLE IF NOT EXISTS access_kicks(group_id INTEGER,user_id INTEGER,state TEXT,PRIMARY KEY(group_id,user_id));''')
            db.execute("CREATE TABLE IF NOT EXISTS access_previews(group_id INTEGER,user_id INTEGER,at REAL,PRIMARY KEY(group_id,user_id))")
            db.execute("CREATE TABLE IF NOT EXISTS access_invites(link TEXT PRIMARY KEY,group_id INTEGER,target INTEGER,actor INTEGER,expires REAL,guest_until REAL,used INTEGER DEFAULT 0)")

    def can_invite(self, role):
        return {"leader": settings.invite_leader, "coLeader": settings.invite_coleader,
                "admin": settings.invite_elder, "member": settings.invite_member}.get(role, False)

    async def join(self, update, context):
        request=update.chat_join_request
        if request.chat.id != settings.clan_group_id or not request.invite_link:
            return
        async with self.lock:
            with self.policy.connect() as db:
                row=db.execute("SELECT target,actor,expires,guest_until,used FROM access_invites WHERE link=? AND group_id=?",(request.invite_link.invite_link,request.chat.id)).fetchone()
            if not row or row[0] != request.from_user.id or row[2] < time.time() or row[4]:
                return
            try:
                roster=await self.roster()
                if not self.can_invite(highest_role(self.accounts(row[1]),roster["memberList"])):
                    return
                # Allowance is persisted before approval: a crash cannot expose a new guest to removal.
                self.policy.guest(request.chat.id,row[0],row[1],row[3],time.time())
                await context.bot.approve_chat_join_request(request.chat.id,row[0])
                with self.policy.connect() as db:
                    db.execute("UPDATE access_invites SET used=1 WHERE link=?",(request.invite_link.invite_link,))
                await context.bot.revoke_chat_invite_link(request.chat.id,request.invite_link.invite_link)
            except Exception:
                log.warning("Guest join request could not be completed; retained for manual review")

    async def roster(self):
        r = await self.client.get(settings.backend_url + "/clan", params={"fresh": "true"})
        r.raise_for_status()
        data = r.json()
        members = data.get("memberList")
        if (data.get("tag") != settings.coc_clan_tag or not isinstance(members,list)
                or not members or data.get("members") != len(members)
                or any(not isinstance(m,dict) or not m.get("tag") for m in members)):
            raise ValueError("Incomplete roster")
        return data

    def accounts(self, user):
        return [b.coc_player_tag for b in self.bindings.get_user_accounts(settings.clan_group_id,user)]

    async def command(self, update, context):
        if update.effective_chat.type != "private":
            await update.effective_message.reply_text("Управление доступом: в личном чате бота /clansync.")
            return
        async with self.lock:
            try:
                roster = await self.roster()
                user = update.effective_user.id
                role = highest_role(self.accounts(user),roster["memberList"])
                args = context.args
                if args and args[0] == "invite":
                    if not self.can_invite(role):
                        await update.effective_message.reply_text("Ваша актуальная игровая роль не разрешает приглашения.")
                        return
                    target=int(args[1]); days=int(args[2])
                    if target <= 0 or not 1 <= days <= 30:
                        raise ValueError("Use a positive Telegram ID and 1–30 days")
                    now=time.time()
                    link=await context.bot.create_chat_invite_link(settings.clan_group_id,expire_date=int(now+3600),creates_join_request=True)
                    with self.policy.connect() as db:
                        db.execute("INSERT INTO access_invites(link,group_id,target,actor,expires,guest_until) VALUES (?,?,?,?,?,?)",(link.invite_link,settings.clan_group_id,target,user,now+3600,now+days*86400))
                    await update.effective_message.reply_text("Приглашение действует час и только для указанного Telegram ID. Гостевой доступ ограничен сроком.\n"+link.invite_link)
                    return
                if role not in {"leader","coLeader"}:
                    await update.effective_message.reply_text("Управление синхронизацией доступно главе и соруководителям.")
                    return
                args = context.args
                if args == ["enable"]:
                    with self.policy.connect() as db:
                        preview=db.execute("SELECT at FROM access_previews WHERE group_id=? AND user_id=?",(settings.clan_group_id,user)).fetchone()
                        if not preview or time.time()-preview[0] > 3600:
                            await update.effective_message.reply_text("Сначала выполните /clansync для свежего предпросмотра.")
                            return
                        db.execute("INSERT OR REPLACE INTO access_activation VALUES (?,?,?)",(settings.clan_group_id,user,time.time()))
                    await update.effective_message.reply_text("Проверка подтверждена. Автоисключение действует только при clan_sync_auto=true в Home Assistant.")
                    return
                if args and args[0] == "guest":
                    target=int(args[1]); days=int(args[2])
                    if days < 0 or days > 365:
                        raise ValueError("Invalid duration")
                    if days == 0 and role != "leader":
                        await update.effective_message.reply_text("Бессрочное разрешение выдаёт только глава.")
                        return
                    now=time.time()
                    self.policy.guest(settings.clan_group_id,target,user,now+days*86400 if days else None,now)
                    await update.effective_message.reply_text("Гостевое разрешение сохранено. 0 дней означает бессрочно.")
                    return
                if args and args[0] == "revoke":
                    self.policy.revoke(settings.clan_group_id,int(args[1]),user,time.time())
                    await update.effective_message.reply_text("Разрешение отозвано. Период подтверждения отсутствия начнётся заново.")
                    return
                if args == ["guests"]:
                    with self.policy.connect() as db:
                        rows=db.execute("SELECT user_id,expires FROM access_guests WHERE group_id=?",(settings.clan_group_id,)).fetchall()
                    await update.effective_message.reply_text("Гости:\n"+"\n".join(f"{u}: {e or 'бессрочно'}" for u,e in rows)[:3500])
                    return
                states=await self.scan(context,roster,False)
                with self.policy.connect() as db:
                    db.execute("INSERT OR REPLACE INTO access_previews VALUES (?,?,?)",(settings.clan_group_id,user,time.time()))
                await update.effective_message.reply_text("Предпросмотр:\n"+"\n".join(f"{u}: {s}" for u,s in states)[:3000]+"\n/clansync enable — подтвердить проверку\n/clansync guest ID ДНИ — разрешить гостя\n/clansync revoke ID — отозвать\n/clansync guests — список")
            except Exception:
                log.warning("Clan access command failed; no automatic action authorized")
                await update.effective_message.reply_text("Проверка не выполнена. Проверьте доступ CoC API и аргументы команды. Удаление не разрешено.")

    async def scan(self, context, roster, execute):
        group=settings.clan_group_id
        states=[]
        for binding in self.bindings.get_bindings_for_group(group):
            user=binding.telegram_user_id
            state=self.policy.observe(group,user,self.accounts(user),roster,settings.coc_clan_tag,time.time(),max(3600,settings.clan_sync_delay_hours*3600))
            states.append((user,state))
            if state != "candidate" or not execute:
                continue
            member=await context.bot.get_chat_member(group,user)
            if member.status in {"creator","administrator","left","kicked"}:
                continue
            try:
                await context.bot.send_message(user,"Все ваши привязанные аккаунты отсутствуют в клане после повторной проверки. Доступ к группе будет закрыт; после возвращения можно вступить снова.")
            except Exception:
                pass
            with self.policy.connect() as db:
                db.execute("INSERT OR REPLACE INTO access_kicks VALUES (?,?,?)",(group,user,"pending"))
            await context.bot.ban_chat_member(group,user)
            await context.bot.unban_chat_member(group,user,only_if_banned=True)
            with self.policy.connect() as db:
                db.execute("UPDATE access_kicks SET state='done' WHERE group_id=? AND user_id=?",(group,user))
                db.execute("INSERT INTO access_events VALUES (?,?,?,?,?)",(group,user,0,"removed",time.time()))
        return states

    async def tick(self, context):
        if not settings.clan_group_id or self.lock.locked():
            return
        async with self.lock:
            try:
                roster=await self.roster()
                me=await context.bot.get_chat_member(settings.clan_group_id,context.bot.id)
                with self.policy.connect() as db:
                    enabled=db.execute("SELECT 1 FROM access_activation WHERE group_id=?",(settings.clan_group_id,)).fetchone()
                permitted=bool(getattr(me,"can_restrict_members",False))
                if permitted:
                    with self.policy.connect() as db:
                        pending=db.execute("SELECT user_id FROM access_kicks WHERE group_id=? AND state='pending'",(settings.clan_group_id,)).fetchall()
                    for (user,) in pending:
                        await context.bot.unban_chat_member(settings.clan_group_id,user,only_if_banned=True)
                        with self.policy.connect() as db:
                            db.execute("UPDATE access_kicks SET state='done' WHERE group_id=? AND user_id=?",(settings.clan_group_id,user))
                if settings.clan_sync_auto and enabled and not permitted:
                    log.warning("Clan sync: Telegram can_restrict_members required")
                await self.scan(context,roster,settings.clan_sync_auto and bool(enabled) and permitted)
            except Exception:
                # Invalidate evidence on errors; stale successes cannot confirm absence.
                with self.policy.connect() as db:
                    db.execute("DELETE FROM access_observations WHERE group_id=?",(settings.clan_group_id,))
                log.warning("Clan sync unavailable; confirmation chain reset")
