import {
  BadRequestException,
  ForbiddenException,
  Injectable,
  NotFoundException,
} from '@nestjs/common';
import * as fs from 'fs';
import * as path from 'path';
import { promises as fsp } from 'fs';
import { mp3ToM4a } from '../split/transcode';
import { Track } from '@prisma/client';
import { PrismaService } from '../prisma/prisma.service';
import { StorageService } from '../storage/storage.service';

type UploadFiles = Record<string, Array<{ path: string; originalname: string; mimetype: string }>>;

const STEM_KEYS = ['bass', 'drums', 'vocals', 'other'] as const;

export interface TrackMetaDto {
  title?: string;
  artist?: string;
  producers?: string;
  featuring?: string;
  year?: string | number;
  releaseDate?: string;
  recordLabel?: string;
  bpm?: string | number;
  songKey?: string;
  durationSec?: string | number;
  visibility?: string;
}

@Injectable()
export class TracksService {
  constructor(
    private readonly prisma: PrismaService,
    private readonly storage: StorageService,
  ) {}

  private toDate(v?: string): Date | null {
    if (!v) return null;
    const d = new Date(v);
    return isNaN(d.getTime()) ? null : d;
  }
  private yearOf(v?: string): number | null {
    const d = this.toDate(v);
    return d ? d.getUTCFullYear() : null;
  }

  private stripId(id: string): string {
    return id.startsWith('ut_') ? id.slice(3) : id;
  }

  private toInt(v: unknown): number | null {
    if (v == null || v === '') return null;
    const n = Number(v);
    return Number.isFinite(n) ? Math.trunc(n) : null;
  }

  private dto(t: Track) {
    return {
      id: t.id,
      songId: `ut_${t.id}`,
      ownerId: t.ownerId,
      title: t.title,
      artist: t.artist,
      producers: t.producers,
      year: t.year,
      recordLabel: t.recordLabel,
      bpm: t.bpm,
      key: t.songKey,
      durationSec: t.durationSec,
      coverUrl: t.coverUrl,
      visibility: t.visibility,
      status: t.status,
      splitSource: t.splitSource,
      valence: t.valence,
      danceability: t.danceability,
      energy: t.energy,
      acousticness: t.acousticness,
      speechiness: t.speechiness,
      instrumentalness: t.instrumentalness,
      analyseTerminee: t.analyseTerminee,
      stems:
        t.status === 'ready'
          ? { bass: t.bassUrl, drums: t.drumsUrl, vocals: t.vocalsUrl, other: t.otherUrl }
          : null,
      createdAt: t.createdAt,
      readyAt: t.readyAt,
    };
  }

  private cleanup(files: UploadFiles): void {
    for (const arr of Object.values(files ?? {})) {
      for (const f of arr) fs.existsSync(f.path) && fs.unlinkSync(f.path);
    }
  }

  async create(ownerId: string, files: UploadFiles, meta: TrackMetaDto) {
    const title = (meta.title ?? '').trim();
    if (!title) {
      this.cleanup(files);
      throw new BadRequestException('title required');
    }
    const visibility = meta.visibility === 'private' ? 'private' : 'public';

    const owner = await this.prisma.user.findUnique({ where: { id: ownerId } });

    let track = await this.prisma.track.create({
      data: {
        ownerId,
        title,
        artist: meta.artist?.trim() || owner?.displayName || owner?.username || null,
        producers: meta.producers?.trim() || null,
        featuring: meta.featuring?.trim() || null,
        year: this.toInt(meta.year) ?? this.yearOf(meta.releaseDate),
        releaseDate: this.toDate(meta.releaseDate),
        recordLabel: meta.recordLabel?.trim() || null,
        bpm: this.toInt(meta.bpm),
        songKey: meta.songKey?.trim() || null,
        durationSec: this.toInt(meta.durationSec),
        visibility,
      },
    });
    const base = `usertracks/${track.id}`;

    try {
      if (files.cover?.[0]) {
        const url = await this.storage.uploadFile(
          `${base}/cover.jpg`,
          files.cover[0].path,
          files.cover[0].mimetype || 'image/jpeg',
        );
        track = await this.prisma.track.update({ where: { id: track.id }, data: { coverUrl: url } });
      }

      const hasAllStems = STEM_KEYS.every((k) => files[k]?.[0]);
      if (hasAllStems) {
        const urls: Record<string, string> = {};
        for (const k of STEM_KEYS) {
          urls[k] = await this.storage.uploadFile(`${base}/${k}.mp3`, files[k][0].path, 'audio/mpeg');
          try {
            const m4a = await mp3ToM4a(await fsp.readFile(files[k][0].path));
            await this.storage.uploadBuffer(`${base}/${k}.m4a`, m4a, 'audio/mp4');
          } catch (e) { /* m4a is best-effort */ }
        }
        track = await this.prisma.track.update({
          where: { id: track.id },
          data: {
            bassUrl: urls.bass,
            drumsUrl: urls.drums,
            vocalsUrl: urls.vocals,
            otherUrl: urls.other,
            status: 'ready',
            splitSource: 'provided',
            readyAt: new Date(),
          },
        });
      } else if (files.audio?.[0]) {
        const ext = path.extname(files.audio[0].originalname) || '.mp3';
        const url = await this.storage.uploadFile(
          `${base}/original${ext}`,
          files.audio[0].path,
          files.audio[0].mimetype || 'audio/mpeg',
        );
        // queued -> picked up by the home GPU worker or Replicate fallback
        track = await this.prisma.track.update({
          where: { id: track.id },
          data: { originalUrl: url, status: 'queued' },
        });
      } else {
        await this.prisma.track.delete({ where: { id: track.id } });
        throw new BadRequestException('provide an audio file or all 4 stems (bass,drums,vocals,other)');
      }
    } finally {
      this.cleanup(files);
    }

    return this.dto(track);
  }

  async detail(viewerId: string, rawId: string) {
    const id = this.stripId(rawId);
    const t = await this.prisma.track.findUnique({ where: { id } });
    if (!t) throw new NotFoundException('track not found');
    if (t.visibility === 'private' && t.ownerId !== viewerId) {
      throw new NotFoundException('track not found');
    }
    return this.dto(t);
  }

  async status(viewerId: string, rawId: string) {
    const t = await this.detail(viewerId, rawId);
    return { id: t.id, status: t.status, splitSource: t.splitSource };
  }

  async list(viewerId: string, ownerId?: string, q?: string, limit = 50) {
    const take = Math.min(Math.max(limit, 1), 100);
    const where: Record<string, unknown> = {};
    if (ownerId) {
      where.ownerId = ownerId;
      if (ownerId !== viewerId) {
        where.visibility = 'public';
        where.status = 'ready';
      }
    } else {
      where.visibility = 'public';
      where.status = 'ready';
      // Catalog tracks live in this table only for the GPU split queue; they're
      // served to the app via the bundled/CDN catalogue. Excluding the catalog
      // owner here stops every catalogue song from appearing TWICE in search
      // (once rich from the catalogue, once as a thin DB row with partial fields).
      const catalogOwner = process.env.CATALOG_USER_ID;
      if (catalogOwner) where.ownerId = { not: catalogOwner };
    }
    if (q?.trim()) where.title = { contains: q.trim(), mode: 'insensitive' };
    const rows = await this.prisma.track.findMany({
      where,
      orderBy: { createdAt: 'desc' },
      take,
    });
    return rows.map((r) => this.dto(r));
  }

  async edit(viewerId: string, rawId: string, meta: TrackMetaDto) {
    const id = this.stripId(rawId);
    const t = await this.prisma.track.findUnique({ where: { id } });
    if (!t) throw new NotFoundException('track not found');
    if (t.ownerId !== viewerId) throw new ForbiddenException('not your track');
    const updated = await this.prisma.track.update({
      where: { id },
      data: {
        title: meta.title?.trim() || t.title,
        artist: meta.artist?.trim() ?? t.artist,
        producers: meta.producers?.trim() ?? t.producers,
        year: meta.year !== undefined ? this.toInt(meta.year) : t.year,
        recordLabel: meta.recordLabel?.trim() ?? t.recordLabel,
        bpm: meta.bpm !== undefined ? this.toInt(meta.bpm) : t.bpm,
        songKey: meta.songKey?.trim() ?? t.songKey,
        visibility: meta.visibility === 'private' ? 'private' : meta.visibility === 'public' ? 'public' : t.visibility,
      },
    });
    return this.dto(updated);
  }

  async remove(viewerId: string, rawId: string) {
    const id = this.stripId(rawId);
    const t = await this.prisma.track.findUnique({ where: { id } });
    if (!t) throw new NotFoundException('track not found');
    if (t.ownerId !== viewerId) throw new ForbiddenException('not your track');
    // Wipe the whole usertracks/<id>/ folder so every object is removed
    // regardless of extension: cover.jpg, the mp3 + m4a stem variants, and the
    // original in whatever format it was uploaded (original.mp3 / original.m4a).
    await this.storage.deleteDirectory(`usertracks/${id}`);
    await this.prisma.track.delete({ where: { id } });
    return { deleted: true };
  }
}
