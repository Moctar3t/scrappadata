import { BadRequestException, Injectable, Logger } from '@nestjs/common';
import { Prisma } from '@prisma/client';
import { PrismaService } from '../prisma/prisma.service';

// Pull-worker queue ops. The worker claims queued rows IMMEDIATELY (no timeout),
// so whenever the home GPU is online it beats the Replicate fallback (which only
// claims rows older than SPLIT_FALLBACK_TIMEOUT_SEC).
@Injectable()
export class InternalSplitService {
  private readonly log = new Logger(InternalSplitService.name);
  constructor(private readonly prisma: PrismaService) {}

  // Enqueue a CATALOG track for the home GPU worker (pull model). Reuses the same
  // `tracks` queue as user uploads; catalog rows are owned by the system catalog
  // user (CATALOG_USER_ID) and carry a YouTube originalUrl the worker yt-dlp's.
  // Idempotent on (catalog owner, originalUrl) so re-running an ingest is safe.
  async enqueueCatalog(dto: {
    youtubeUrl?: string; title?: string; artist?: string; bpm?: number;
    songKey?: string; durationSec?: number; coverUrl?: string; year?: number;
    releaseDate?: string; recordLabel?: string;
    producers?: string; featuring?: string; writers?: string;
  }) {
    const ownerId = process.env.CATALOG_USER_ID;
    if (!ownerId) throw new BadRequestException('CATALOG_USER_ID not configured');
    if (!dto?.youtubeUrl || !dto?.title)
      throw new BadRequestException('youtubeUrl and title are required');
    const existing = await this.prisma.track.findFirst({
      where: { ownerId, originalUrl: dto.youtubeUrl },
      select: { id: true, status: true },
    });
    if (existing) return { id: existing.id, status: existing.status, dedup: true };
    const t = await this.prisma.track.create({
      data: {
        ownerId,
        title: dto.title,
        artist: dto.artist ?? null,
        bpm: dto.bpm ?? null,
        songKey: dto.songKey ?? null,
        durationSec: dto.durationSec ?? null,
        coverUrl: dto.coverUrl ?? null,
        year: dto.year ?? null,
        releaseDate: dto.releaseDate ? new Date(dto.releaseDate) : null,
        recordLabel: dto.recordLabel ?? null,
        producers: dto.producers ?? null,
        featuring: dto.featuring ?? null,
        writers: dto.writers ?? null,
        originalUrl: dto.youtubeUrl,
        visibility: 'public',
        status: 'queued',
      },
      select: { id: true },
    });
    
    // Boundary Verification Logging
    const pCount = dto.producers ? dto.producers.split(';').length : 0;
    const wCount = dto.writers ? dto.writers.split(';').length : 0;
    this.log.log(`catalog enqueued ${t.id} (${dto.title}) [producers=${pCount}, writers=${wCount}]`);
    
    return { id: t.id, status: 'queued' };
  }

  async claimNext(): Promise<{ id: string; originalUrl: string } | Record<string, never>> {
    const rows = await this.prisma.$queryRaw<Array<{ id: string; original_url: string }>>(Prisma.sql`
      UPDATE tracks SET status='processing', claimed_by='gpu', claimed_at=now()
      WHERE id = (
        SELECT id FROM tracks
        WHERE status='queued' AND original_url IS NOT NULL
        ORDER BY created_at
        FOR UPDATE SKIP LOCKED LIMIT 1)
      RETURNING id, original_url`);
    if (rows.length === 0) return {};
    this.log.log(`gpu worker claimed ${rows[0].id}`);
    return { id: rows[0].id, originalUrl: rows[0].original_url };
  }

  async complete(id: string, stems: Record<string, string>) {
    for (const k of ['bass', 'drums', 'vocals', 'other']) {
      if (!stems?.[k]) throw new BadRequestException(`missing stem url: ${k}`);
    }
    const r = await this.prisma.track.updateMany({
      where: { id, status: 'processing' },
      data: {
        bassUrl: stems.bass, drumsUrl: stems.drums, vocalsUrl: stems.vocals, otherUrl: stems.other,
        status: 'ready', splitSource: 'gpu', readyAt: new Date(), error: null,
        claimedBy: null, claimedAt: null, ...(stems.bpm ? { bpm: Number(stems.bpm) } : {}), ...(stems.songKey ? { songKey: stems.songKey } : {}),
      },
    });
    if (r.count === 1) this.log.log(`gpu worker completed ${id}`);
    return { ok: r.count === 1 };
  }

  async fail(id: string, error: string) {
    const t = await this.prisma.track.findUnique({ where: { id } });
    if (!t || t.status !== 'processing') return { ok: false };
    const max = Number(process.env.SPLIT_MAX_ATTEMPTS ?? 5);
    const attempts = t.attempts + 1;
    const failed = attempts >= max;
    await this.prisma.track.update({
      where: { id },
      data: {
        status: failed ? 'failed' : 'queued',
        attempts,
        error: (error ?? '').slice(0, 500),
        claimedBy: null,
        claimedAt: null,
      },
    });
    this.log.warn(`gpu worker ${failed ? 'FAILED' : 'requeued'} ${id} (attempt ${attempts})`);
    return { ok: true };
  }
}
