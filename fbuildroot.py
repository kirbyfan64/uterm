from fbuild.builders.pkg_config import PkgConfig
from fbuild.builders.platform import guess_platform
from fbuild.builders import find_program
from fbuild.builders.cxx import guess as guess_cxx
from fbuild.builders.c import guess as guess_c
from fbuild.record import Record
from fbuild.path import Path
import fbuild.db


def arguments(parser):
    group = parser.add_argument_group('config options')
    group.add_argument('--cc', help='Use the given C compiler')
    group.add_argument('--cflag', help='Pass the given flag to the C compiler',
                       action='append', default=[])
    group.add_argument('--cxx', help='Use the given C++ compiler')
    group.add_argument('--cxxflag', help='Pass the given flag to the C++ compiler',
                       action='append', default=[])
    group.add_argument('--no-force-color',
                       help='Disable forced C++ compiler colored output',
                       action='store_true', default=False)
    group.add_argument('--release', help='Build in release mode', action='store_true',
                       default=False)
    group.add_argument('--ld',
                       help='The name of the linker to try to use. Default is ' \
                             'lld for Clang and gold for other compilers.')


def truthy(lst):
    return list(filter(bool, lst))


@fbuild.db.caches
def pkg_config(ctx, package, *, name=None, optional=False, suffix=''):
    name = name or package
    if suffix:
        suffix = ' %s' % suffix

    pkg = PkgConfig(ctx, package)
    ctx.logger.check('checking for %s' % name)
    try:
        rec = Record(cflags=truthy(pkg.cflags()), ldlibs=truthy(pkg.libs()))
    except fbuild.Error:
        ctx.logger.failed()
        if not optional:
            raise fbuild.Error('%s is required%s.' % (name, suffix))
    else:
        ctx.logger.passed()
        return rec


@fbuild.db.caches
def configure(ctx):
    platform = guess_platform(ctx)

    posix_flags = ['-Wno-unused-command-line-argument']
    clang_flags = []
    nonclang_flags = []
    kw = {}

    if not platform & {'linux', 'macosx'}:
        raise fbuild.ConfigFailed('Only Mac and Linux are currently supported.')

    if ctx.options.ld is not None:
        # Shortcut it to avoid issues on old systems (e.g. CentOS 6).
        if ctx.options.ld != 'bfd':
            posix_flags.append('-fuse-ld=%s' % ctx.options.ld)
    else:
        clang_flags.append('-fuse-ld=lld')
        nonclang_flags.append('-fuse-ld=gold')

    if ctx.options.release:
        kw['optimize'] = True
        posix_flags.append('-flto')
    else:
        kw['debug'] = True
        clang_flags.append('-fno-limit-debug-info')

    if not ctx.options.no_force_color:
        posix_flags.append('-fdiagnostics-color')

    c = guess_c.static(ctx, exe=ctx.options.cc, flags=ctx.options.cflag,
                       platform_options=[
                            ({'posix'}, {'flags+': posix_flags}),
                            ({'clang'}, {'flags+': clang_flags}),
                       ], **kw)

    cxx = guess_cxx.static(ctx, exe=ctx.options.cxx, flags=ctx.options.cxxflag,
                           platform_options=[
                            ({'posix'}, {'flags+': ['-std=c++11'] + posix_flags}),
                            ({'clang++'}, {'flags+': clang_flags,
                                            'macros':
                                                ['__CLANG_SUPPORT_DYN_ANNOTATION__']}),
                            ({'!clang++'}, {'flags+': nonclang_flags}),
                           ], **kw)

    xkbcommon = pkg_config(ctx, 'xkbcommon', optional=True)
    glfw = pkg_config(ctx, 'glfw3', name='GLFW3')
    egl = pkg_config(ctx, 'egl', name='EGL')
    confuse = pkg_config(ctx, 'libconfuse')

    if platform & {'linux'}:
        freetype = pkg_config(ctx, 'freetype2')
        fontconfig = pkg_config(ctx, 'fontconfig')
    else:
        freetype = fontconfig = None

    return Record(platform=platform, c=c, cxx=cxx, xkbcommon=xkbcommon, glfw=glfw,
                  egl=egl, confuse=confuse, freetype=freetype, fontconfig=fontconfig)


def prefixed_sources(prefix, paths, glob=False, ignore=None):
    files = []
    prefix = Path(prefix)

    for path in paths:
        path = prefix / path

        if glob:
            for subpath in path.iglob():
                if ignore is None or ignore not in subpath:
                    files.append(subpath)
        else:
            files.append(path)

    return files


def abseil_sources(*globs):
    return prefixed_sources('deps/abseil/absl', globs, glob=True, ignore='_test')


def build_abseil(ctx, cxx):
    abseil = Record(includes=['deps/abseil'])

    abseil.base = cxx.build_lib('abseil_base',
                                abseil_sources('base/*.cc', 'base/internal/*.cc'),
                                includes=abseil.includes,
                                include_source_dirs=False)

    abseil.numeric = cxx.build_lib('abseil_numeric', abseil_sources('numeric/int128.cc'),
                                   includes=abseil.includes,
                                   libs=[abseil.base])

    abseil.strings = cxx.build_lib('abseil_strings',
                                   abseil_sources('strings/*.cc',
                                                  'strings/internal/*.cc'),
                                   includes=abseil.includes,
                                   libs=[abseil.base, abseil.numeric])

    abseil.stacktrace = cxx.build_lib('abseil_stacktrace',
                                      abseil_sources('debugging/stacktrace.cc',
                                                     'debugging/internal/*.cc'),
                                      includes=abseil.includes)

    return abseil


@fbuild.db.caches
def generate_gl3w(ctx):
    outdir = ctx.buildroot / 'gl3w'

    python3 = find_program(ctx, ['python3', 'python2', 'python'])
    cmd = [python3, 'deps/gl3w/gl3w_gen.py', '--root', outdir]

    ctx.execute(cmd, 'gl3w_gen.py', 'gl3w.c gl3w.h glcorearb.h', color='compile',
                stdout_quieter=1)
    ctx.db.add_external_dependencies_to_call(
        srcs=['deps/gl3w/gl3w_gen.py'],
        dsts=[outdir / 'include' / 'GL' / 'gl3w.h',
              outdir / 'include' / 'GL' / 'glcorearb.h'],
    )
    return outdir / 'include', outdir / 'src' / 'gl3w.c'


def build_gl3w(ctx, c):
    include, src = generate_gl3w(ctx)
    return Record(includes=[include], lib=c.build_lib('gl3w', [src], includes=[include]))


def build_fmtlib(ctx, cxx):
    fmt = Path('deps/fmt')
    return Record(includes=[fmt], lib=cxx.build_lib('fmt', [fmt / 'fmt' / 'format.cc'],
                                                    include_source_dirs=False))


def build_libtsm(ctx, c, xkbcommon):
    base = Path('deps/libtsm')
    src = base / 'src'
    shl = src / 'shared'
    tsm = src / 'tsm'

    sources = Path.glob(tsm / '*.c') + [shl / 'shl-htable.c',
                                        base / 'external' / 'wcwidth.c']
    includes = [shl, tsm, base]

    if xkbcommon is not None:
        cflags = xkbcommon.cflags
    else:
        cflags = []

    macros = ['_GNU_SOURCE=1']
    if not ctx.options.release:
        macros.append('BUILD_ENABLE_DEBUG')

    return Record(includes=includes, lib=c.build_lib('tsm', sources, includes=includes,
                                                     macros=macros, cflags=cflags))


def skia_sources(*globs):
    return prefixed_sources('deps/skia/src', globs)


def build_skia(ctx, platform, cxx, freetype, fontconfig):
    srcs = [
        # core
        'c/sk_paint.cpp',
        'c/sk_surface.cpp',
        'core/SkAAClip.cpp',
        'core/SkAnnotation.cpp',
        'core/SkAlphaRuns.cpp',
        'core/SkATrace.cpp',
        'core/SkAutoPixmapStorage.cpp',
        'core/SkBBHFactory.cpp',
        'core/SkBigPicture.cpp',
        'core/SkBitmap.cpp',
        'core/SkBitmapCache.cpp',
        'core/SkBitmapController.cpp',
        'core/SkBitmapDevice.cpp',
        'core/SkBitmapProcState.cpp',
        'core/SkBitmapProcState_matrixProcs.cpp',
        'core/SkBitmapProvider.cpp',
        'core/SkBlendMode.cpp',
        'core/SkBlitMask_D32.cpp',
        'core/SkBlitRow_D32.cpp',
        'core/SkBlitter.cpp',
        'core/SkBlitter_A8.cpp',
        'core/SkBlitter_ARGB32.cpp',
        'core/SkBlitter_RGB565.cpp',
        'core/SkBlitter_Sprite.cpp',
        'core/SkBlurImageFilter.cpp',
        'core/SkBuffer.cpp',
        'core/SkCachedData.cpp',
        'core/SkCanvas.cpp',
        'core/SkCanvasPriv.cpp',
        'core/SkCoverageDelta.cpp',
        'core/SkClipStack.cpp',
        'core/SkClipStackDevice.cpp',
        'core/SkColor.cpp',
        'core/SkColorFilter.cpp',
        'core/SkColorLookUpTable.cpp',
        'core/SkColorMatrixFilterRowMajor255.cpp',
        'core/SkColorSpace.cpp',
        'core/SkColorSpace_A2B.cpp',
        'core/SkColorSpace_New.cpp',
        'core/SkColorSpace_XYZ.cpp',
        'core/SkColorSpace_ICC.cpp',
        'core/SkColorSpaceXform.cpp',
        'core/SkColorSpaceXformCanvas.cpp',
        'core/SkColorSpaceXformer.cpp',
        'core/SkColorSpaceXformImageGenerator.cpp',
        'core/SkColorSpaceXform_A2B.cpp',
        'core/SkColorTable.cpp',
        'core/SkConvertPixels.cpp',
        'core/SkCpu.cpp',
        'core/SkCubicClipper.cpp',
        'core/SkCubicMap.cpp',
        'core/SkData.cpp',
        'core/SkDataTable.cpp',
        'core/SkDebug.cpp',
        'core/SkDeferredDisplayListRecorder.cpp',
        'core/SkDeque.cpp',
        'core/SkDevice.cpp',
        'core/SkDeviceLooper.cpp',
        'core/SkDeviceProfile.cpp',
        'lazy/SkDiscardableMemoryPool.cpp',
        'core/SkDistanceFieldGen.cpp',
        'core/SkDither.cpp',
        'core/SkDocument.cpp',
        'core/SkDraw.cpp',
        'core/SkDraw_vertices.cpp',
        'core/SkDrawable.cpp',
        'core/SkDrawLooper.cpp',
        'core/SkDrawShadowInfo.cpp',
        'core/SkEdgeBuilder.cpp',
        'core/SkEdgeClipper.cpp',
        'core/SkExecutor.cpp',
        'core/SkAnalyticEdge.cpp',
        'core/SkFDot6Constants.cpp',
        'core/SkEdge.cpp',
        'core/SkArenaAlloc.cpp',
        'core/SkGaussFilter.cpp',
        'core/SkFlattenable.cpp',
        'core/SkFlattenableSerialization.cpp',
        'core/SkFont.cpp',
        'core/SkFontLCDConfig.cpp',
        'core/SkFontMgr.cpp',
        'core/SkFontDescriptor.cpp',
        'core/SkFontStream.cpp',
        'core/SkGeometry.cpp',
        'core/SkGlobalInitialization_core.cpp',
        'core/SkGlyphCache.cpp',
        'core/SkGpuBlurUtils.cpp',
        'core/SkGraphics.cpp',
        'core/SkHalf.cpp',
        'core/SkICC.cpp',
        'core/SkImageFilter.cpp',
        'core/SkImageFilterCache.cpp',
        'core/SkImageInfo.cpp',
        'core/SkImageGenerator.cpp',
        'core/SkLineClipper.cpp',
        'core/SkLiteDL.cpp',
        'core/SkLiteRecorder.cpp',
        'core/SkLocalMatrixImageFilter.cpp',
        'core/SkMD5.cpp',
        'core/SkMallocPixelRef.cpp',
        'core/SkMask.cpp',
        'core/SkMaskBlurFilter.cpp',
        'core/SkMaskCache.cpp',
        'core/SkMaskFilter.cpp',
        'core/SkMaskGamma.cpp',
        'core/SkMath.cpp',
        'core/SkMatrix.cpp',
        'core/SkMatrix44.cpp',
        'core/SkMatrixImageFilter.cpp',
        'core/SkMetaData.cpp',
        'core/SkMipMap.cpp',
        'core/SkMiniRecorder.cpp',
        'core/SkModeColorFilter.cpp',
        'core/SkMultiPictureDraw.cpp',
        'core/SkLatticeIter.cpp',
        'core/SkOpts.cpp',
        'core/SkOverdrawCanvas.cpp',
        'core/SkPaint.cpp',
        'core/SkPaintPriv.cpp',
        'core/SkPath.cpp',
        'core/SkPathEffect.cpp',
        'core/SkPathMeasure.cpp',
        'core/SkPathRef.cpp',
        'core/SkPicture.cpp',
        'core/SkPictureContentInfo.cpp',
        'core/SkPictureData.cpp',
        'core/SkPictureFlat.cpp',
        'core/SkPictureImageGenerator.cpp',
        'core/SkPicturePlayback.cpp',
        'core/SkPictureRecord.cpp',
        'core/SkPictureRecorder.cpp',
        'core/SkPixelRef.cpp',
        'core/SkPixmap.cpp',
        'core/SkPoint.cpp',
        'core/SkPoint3.cpp',
        'core/SkPtrRecorder.cpp',
        'core/SkQuadClipper.cpp',
        'core/SkRasterClip.cpp',
        'core/SkRasterPipeline.cpp',
        'core/SkRasterPipelineBlitter.cpp',
        'core/SkRasterizer.cpp',
        'core/SkReadBuffer.cpp',
        'core/SkRecord.cpp',
        'core/SkRecords.cpp',
        'core/SkRecordDraw.cpp',
        'core/SkRecordOpts.cpp',
        'core/SkRecordedDrawable.cpp',
        'core/SkRecorder.cpp',
        'core/SkRect.cpp',
        'core/SkRefDict.cpp',
        'core/SkRegion.cpp',
        'core/SkRegion_path.cpp',
        'core/SkResourceCache.cpp',
        'core/SkRRect.cpp',
        'core/SkRTree.cpp',
        'core/SkRWBuffer.cpp',
        'core/SkScalar.cpp',
        'core/SkScalerContext.cpp',
        'core/SkScan.cpp',
        'core/SkScan_AAAPath.cpp',
        'core/SkScan_DAAPath.cpp',
        'core/SkScan_AntiPath.cpp',
        'core/SkScan_Antihair.cpp',
        'core/SkScan_Hairline.cpp',
        'core/SkScan_Path.cpp',
        'core/SkSemaphore.cpp',
        'core/SkSharedMutex.cpp',
        'core/SkSpecialImage.cpp',
        'core/SkSpecialSurface.cpp',
        'core/SkSpinlock.cpp',
        'core/SkSpriteBlitter_ARGB32.cpp',
        'core/SkSpriteBlitter_RGB565.cpp',
        'core/SkStream.cpp',
        'core/SkString.cpp',
        'core/SkStringUtils.cpp',
        'core/SkStroke.cpp',
        'core/SkStrokeRec.cpp',
        'core/SkStrokerPriv.cpp',
        'core/SkSwizzle.cpp',
        'core/SkSRGB.cpp',
        'core/SkTaskGroup.cpp',
        'core/SkTaskGroup2D.cpp',
        'core/SkTextBlob.cpp',
        'core/SkTime.cpp',
        'core/SkThreadID.cpp',
        'core/SkTLS.cpp',
        'core/SkTSearch.cpp',
        'core/SkTypeface.cpp',
        'core/SkTypefaceCache.cpp',
        'core/SkUnPreMultiply.cpp',
        'core/SkUtils.cpp',
        'core/SkVertices.cpp',
        'core/SkVertState.cpp',
        'core/SkWriteBuffer.cpp',
        'core/SkWriter32.cpp',
        'core/SkXfermode.cpp',
        'core/SkXfermodeInterpretation.cpp',
        'core/SkYUVPlanesCache.cpp',

        'image/SkImage.cpp',
        'image/SkImage_Lazy.cpp',
        'image/SkImage_Raster.cpp',
        'image/SkSurface.cpp',
        'image/SkSurface_Raster.cpp',

        'pipe/SkPipeCanvas.cpp',
        'pipe/SkPipeReader.cpp',

        'shaders/SkBitmapProcShader.cpp',
        'shaders/SkColorFilterShader.cpp',
        'shaders/SkColorShader.cpp',
        'shaders/SkComposeShader.cpp',
        'shaders/SkImageShader.cpp',
        'shaders/SkLocalMatrixShader.cpp',
        'shaders/SkPictureShader.cpp',
        'shaders/SkShader.cpp',

        'pathops/SkAddIntersections.cpp',
        'pathops/SkDConicLineIntersection.cpp',
        'pathops/SkDCubicLineIntersection.cpp',
        'pathops/SkDCubicToQuads.cpp',
        'pathops/SkDLineIntersection.cpp',
        'pathops/SkDQuadLineIntersection.cpp',
        'pathops/SkIntersections.cpp',
        'pathops/SkOpAngle.cpp',
        'pathops/SkOpBuilder.cpp',
        'pathops/SkOpCoincidence.cpp',
        'pathops/SkOpContour.cpp',
        'pathops/SkOpCubicHull.cpp',
        'pathops/SkOpEdgeBuilder.cpp',
        'pathops/SkOpSegment.cpp',
        'pathops/SkOpSpan.cpp',
        'pathops/SkPathOpsCommon.cpp',
        'pathops/SkPathOpsConic.cpp',
        'pathops/SkPathOpsCubic.cpp',
        'pathops/SkPathOpsCurve.cpp',
        'pathops/SkPathOpsDebug.cpp',
        'pathops/SkPathOpsLine.cpp',
        'pathops/SkPathOpsOp.cpp',
        'pathops/SkPathOpsPoint.cpp',
        'pathops/SkPathOpsQuad.cpp',
        'pathops/SkPathOpsRect.cpp',
        'pathops/SkPathOpsSimplify.cpp',
        'pathops/SkPathOpsTSect.cpp',
        'pathops/SkPathOpsTightBounds.cpp',
        'pathops/SkPathOpsTypes.cpp',
        'pathops/SkPathOpsWinding.cpp',
        'pathops/SkPathWriter.cpp',
        'pathops/SkReduceOrder.cpp',

        'jumper/SkJumper.cpp',
        'jumper/SkJumper_stages.cpp',
        'jumper/SkJumper_stages_lowp.cpp',
        'jumper/SkJumper_generated.S',

        # utils
        'utils/SkBase64.cpp',
        'utils/SkFrontBufferedStream.cpp',
        'utils/SkCamera.cpp',
        'utils/SkCanvasStack.cpp',
        'utils/SkCanvasStateUtils.cpp',
        'utils/SkDashPath.cpp',
        'utils/SkDumpCanvas.cpp',
        'utils/SkEventTracer.cpp',
        'utils/SkFloatToDecimal.cpp',
        'utils/SkInsetConvexPolygon.cpp',
        'utils/SkInterpolator.cpp',
        'utils/SkJSONWriter.cpp',
        'utils/SkMatrix22.cpp',
        'utils/SkMultiPictureDocument.cpp',
        'utils/SkNWayCanvas.cpp',
        'utils/SkNullCanvas.cpp',
        'utils/SkOSPath.cpp',
        'utils/SkPaintFilterCanvas.cpp',
        'utils/SkParse.cpp',
        'utils/SkParseColor.cpp',
        'utils/SkParsePath.cpp',
        'utils/SkPatchUtils.cpp',
        'utils/SkShadowTessellator.cpp',
        'utils/SkShadowUtils.cpp',
        'utils/SkTextBox.cpp',
        'utils/SkThreadUtils_pthread.cpp',
        'utils/SkWhitelistTypefaces.cpp',

        # xps
        'xps/SkXPSDocument.cpp',
        'xps/SkXPSDevice.cpp',

        # others
        'codec/SkBmpBaseCodec.cpp',
        'codec/SkBmpCodec.cpp',
        'codec/SkBmpMaskCodec.cpp',
        'codec/SkBmpRLECodec.cpp',
        'codec/SkBmpStandardCodec.cpp',
        'codec/SkCodec.cpp',
        'codec/SkCodecImageGenerator.cpp',
        'codec/SkGifCodec.cpp',
        'codec/SkMaskSwizzler.cpp',
        'codec/SkMasks.cpp',
        'codec/SkSampledCodec.cpp',
        'codec/SkSampler.cpp',
        'codec/SkStreamBuffer.cpp',
        'codec/SkSwizzler.cpp',
        'codec/SkWbmpCodec.cpp',
        'images/SkImageEncoder.cpp',
        'ports/SkDiscardableMemory_none.cpp',
        'ports/SkImageGenerator_skia.cpp',
        'ports/SkMemory_malloc.cpp',
        'ports/SkOSFile_stdio.cpp',
        'ports/SkOSFile_posix.cpp',
        'ports/SkTLS_pthread.cpp',
        'sfnt/SkOTTable_name.cpp',
        'sfnt/SkOTUtils.cpp',
        'ports/SkDebug_stdio.cpp',

        # opts
        'opts/SkBlitRow_opts_none.cpp',
        'opts/SkBlitMask_opts_none.cpp',
        'opts/SkBitmapProcState_opts_none.cpp',
        # 'opts/SkBitmapProcState_opts_SSE2.cpp',
        # 'opts/SkBlitRow_opts_SSE2.cpp',
        # 'opts/SkBlitMask_opts_none.cpp',

        # 'opts/SkBitmapProcState_opts_SSSE3.cpp',
        'opts/SkOpts_ssse3.cpp',

        'opts/SkOpts_sse41.cpp',
        'opts/SkOpts_sse42.cpp',
        'opts/SkOpts_avx.cpp',
        # 'opts/opts_check_x86.cpp',

        # effects
        'ports/SkGlobalInitialization_none.cpp',

        # gpu
        'gpu/GrAuditTrail.cpp',
        'gpu/GrBackendSurface.cpp',
        'gpu/GrBackendTextureImageGenerator.cpp',
        'gpu/GrAHardwareBufferImageGenerator.cpp',
        'gpu/GrBitmapTextureMaker.cpp',
        'gpu/GrBlend.cpp',
        'gpu/GrBlurUtils.cpp',
        'gpu/GrBuffer.cpp',
        'gpu/GrBufferAllocPool.cpp',
        'gpu/GrCaps.cpp',
        'gpu/GrClipStackClip.cpp',
        'gpu/GrColorSpaceInfo.cpp',
        'gpu/GrColorSpaceXform.cpp',
        'gpu/GrContext.cpp',
        'gpu/GrDefaultGeoProcFactory.cpp',
        'gpu/GrDistanceFieldGenFromVector.cpp',
        'gpu/GrDrawingManager.cpp',
        'gpu/GrDrawOpAtlas.cpp',
        'gpu/GrDrawOpTest.cpp',
        'gpu/GrFixedClip.cpp',
        'gpu/GrFragmentProcessor.cpp',
        'gpu/GrGpu.cpp',
        'gpu/GrGpuCommandBuffer.cpp',
        'gpu/GrGpuResource.cpp',
        'gpu/GrGpuFactory.cpp',
        'gpu/GrImageTextureMaker.cpp',
        'gpu/GrMemoryPool.cpp',
        'gpu/GrOpFlushState.cpp',
        'gpu/GrOpList.cpp',
        'gpu/GrPaint.cpp',
        'gpu/GrPath.cpp',
        'gpu/GrPathProcessor.cpp',
        'gpu/GrPathRange.cpp',
        'gpu/GrPathRendererChain.cpp',
        'gpu/GrPathRenderer.cpp',
        'gpu/GrPathRendering.cpp',
        'gpu/GrPathUtils.cpp',
        'gpu/GrOnFlushResourceProvider.cpp',
        'gpu/GrPipeline.cpp',
        'gpu/GrPrimitiveProcessor.cpp',
        'gpu/GrProcessorSet.cpp',
        'gpu/GrProgramDesc.cpp',
        'gpu/GrProcessor.cpp',
        'gpu/GrProcessorAnalysis.cpp',
        'gpu/GrProcessorUnitTest.cpp',
        'gpu/GrProxyProvider.cpp',
        'gpu/GrGpuResourceRef.cpp',
        'gpu/GrRectanizer_pow2.cpp',
        'gpu/GrRectanizer_skyline.cpp',
        'gpu/GrRenderTarget.cpp',
        'gpu/GrRenderTargetProxy.cpp',
        'gpu/GrReducedClip.cpp',
        'gpu/GrRenderTargetContext.cpp',
        'gpu/GrPathRenderingRenderTargetContext.cpp',
        'gpu/GrRenderTargetOpList.cpp',
        'gpu/GrResourceAllocator.cpp',
        'gpu/GrResourceCache.cpp',
        'gpu/GrResourceProvider.cpp',
        'gpu/GrShaderCaps.cpp',
        'gpu/GrShape.cpp',
        'gpu/GrStencilAttachment.cpp',
        'gpu/GrStencilSettings.cpp',
        'gpu/GrStyle.cpp',
        'gpu/GrTessellator.cpp',
        'gpu/GrTextureOpList.cpp',
        'gpu/GrTestUtils.cpp',
        'gpu/GrShaderVar.cpp',
        'gpu/GrSKSLPrettyPrint.cpp',
        'gpu/GrSoftwarePathRenderer.cpp',
        'gpu/GrSurface.cpp',
        'gpu/GrSurfaceContext.cpp',
        'gpu/GrSurfaceProxy.cpp',
        'gpu/GrSWMaskHelper.cpp',
        'gpu/GrTexture.cpp',
        'gpu/GrTextureAdjuster.cpp',
        'gpu/GrTextureContext.cpp',
        'gpu/GrTextureMaker.cpp',
        'gpu/GrTextureProducer.cpp',
        'gpu/GrTextureProxy.cpp',
        'gpu/GrTextureRenderTargetProxy.cpp',
        'gpu/GrXferProcessor.cpp',
        'gpu/GrYUVProvider.cpp',

        'gpu/ops/GrAAConvexTessellator.cpp',
        'gpu/ops/GrAAConvexPathRenderer.cpp',
        'gpu/ops/GrAAFillRectOp.cpp',
        'gpu/ops/GrAAHairLinePathRenderer.cpp',
        'gpu/ops/GrAALinearizingConvexPathRenderer.cpp',
        'gpu/ops/GrAAStrokeRectOp.cpp',
        'gpu/ops/GrAtlasTextOp.cpp',
        'gpu/ops/GrClearOp.cpp',
        'gpu/ops/GrCopySurfaceOp.cpp',
        'gpu/ops/GrDashLinePathRenderer.cpp',
        'gpu/ops/GrDashOp.cpp',
        'gpu/ops/GrDefaultPathRenderer.cpp',
        'gpu/ops/GrDrawAtlasOp.cpp',
        'gpu/ops/GrDrawPathOp.cpp',
        'gpu/ops/GrDrawVerticesOp.cpp',
        'gpu/ops/GrMeshDrawOp.cpp',
        'gpu/ops/GrMSAAPathRenderer.cpp',
        'gpu/ops/GrNonAAFillRectOp.cpp',
        'gpu/ops/GrNonAAStrokeRectOp.cpp',
        'gpu/ops/GrLatticeOp.cpp',
        'gpu/ops/GrOp.cpp',
        'gpu/ops/GrOvalOpFactory.cpp',
        'gpu/ops/GrRegionOp.cpp',
        'gpu/ops/GrSemaphoreOp.cpp',
        'gpu/ops/GrShadowRRectOp.cpp',
        'gpu/ops/GrSimpleMeshDrawOpHelper.cpp',
        'gpu/ops/GrSmallPathRenderer.cpp',
        'gpu/ops/GrStencilAndCoverPathRenderer.cpp',
        'gpu/ops/GrStencilPathOp.cpp',
        'gpu/ops/GrTessellatingPathRenderer.cpp',
        'gpu/ops/GrTextureOp.cpp',

        'gpu/ccpr/GrCCAtlas.cpp',
        'gpu/ccpr/GrCCClipProcessor.cpp',
        'gpu/ccpr/GrCCCoverageOp.cpp',
        'gpu/ccpr/GrCCCoverageProcessor.cpp',
        # ??
        'gpu/ccpr/GrCCCoverageProcessor_GSImpl.cpp',
        'gpu/ccpr/GrCCCoverageProcessor_VSImpl.cpp',
        'gpu/ccpr/GrCCCubicShader.cpp',
        'gpu/ccpr/GrCCGeometry.cpp',
        'gpu/ccpr/GrCCPathProcessor.cpp',
        'gpu/ccpr/GrCCQuadraticShader.cpp',
        'gpu/ccpr/GrCCTriangleShader.cpp',
        'gpu/ccpr/GrCoverageCountingPathRenderer.cpp',

        'gpu/effects/GrAARectEffect.cpp',
        'gpu/effects/GrArithmeticFP.cpp',
        'gpu/effects/GrBlurredEdgeFragmentProcessor.cpp',
        'gpu/effects/GrCircleEffect.cpp',
        'gpu/effects/GrConfigConversionEffect.cpp',
        'gpu/effects/GrConstColorProcessor.cpp',
        'gpu/effects/GrCoverageSetOpXP.cpp',
        'gpu/effects/GrCustomXfermode.cpp',
        'gpu/effects/GrBezierEffect.cpp',
        'gpu/effects/GrConvexPolyEffect.cpp',
        'gpu/effects/GrBicubicEffect.cpp',
        'gpu/effects/GrBitmapTextGeoProc.cpp',
        'gpu/effects/GrDisableColorXP.cpp',
        'gpu/effects/GrDistanceFieldGeoProc.cpp',
        'gpu/effects/GrDitherEffect.cpp',
        'gpu/effects/GrEllipseEffect.cpp',
        'gpu/effects/GrGaussianConvolutionFragmentProcessor.cpp',
        'gpu/effects/GrLumaColorFilterEffect.cpp',
        'gpu/effects/GrMagnifierEffect.cpp',
        'gpu/effects/GrMatrixConvolutionEffect.cpp',
        'gpu/effects/GrNonlinearColorSpaceXformEffect.cpp',
        'gpu/effects/GrOverdrawFragmentProcessor.cpp',
        'gpu/effects/GrOvalEffect.cpp',
        'gpu/effects/GrPorterDuffXferProcessor.cpp',
        'gpu/effects/GrPremulInputFragmentProcessor.cpp',
        'gpu/effects/GrRectBlurEffect.cpp',
        'gpu/effects/GrRRectBlurEffect.cpp',
        'gpu/effects/GrRRectEffect.cpp',
        'gpu/effects/GrShadowGeoProc.cpp',
        'gpu/effects/GrSimpleTextureEffect.cpp',
        'gpu/effects/GrSRGBEffect.cpp',
        'gpu/effects/GrTextureDomain.cpp',
        'gpu/effects/GrTextureStripAtlas.cpp',
        'gpu/effects/GrUnpremulInputFragmentProcessor.cpp',
        'gpu/effects/GrXfermodeFragmentProcessor.cpp',
        'gpu/effects/GrYUVtoRGBEffect.cpp',

        'gpu/text/GrAtlasGlyphCache.cpp',
        'gpu/text/GrAtlasTextBlob.cpp',
        'gpu/text/GrAtlasTextBlobVertexRegenerator.cpp',
        'gpu/text/GrAtlasTextContext.cpp',
        'gpu/text/GrDistanceFieldAdjustTable.cpp',
        'gpu/text/GrStencilAndCoverTextContext.cpp',
        'gpu/text/GrTextBlobCache.cpp',
        'gpu/text/GrTextUtils.cpp',

        'gpu/gl/GrGLAssembleInterface.cpp',
        'gpu/gl/GrGLBuffer.cpp',
        'gpu/gl/GrGLCaps.cpp',
        'gpu/gl/GrGLContext.cpp',
        'gpu/gl/GrGLCreateNullInterface.cpp',
        'gpu/gl/GrGLGLSL.cpp',
        'gpu/gl/GrGLGpu.cpp',
        'gpu/gl/GrGLGpuCommandBuffer.cpp',
        'gpu/gl/GrGLGpuProgramCache.cpp',
        'gpu/gl/GrGLExtensions.cpp',
        'gpu/gl/GrGLInterface.cpp',
        'gpu/gl/GrGLPath.cpp',
        'gpu/gl/GrGLPathRange.cpp',
        'gpu/gl/GrGLPathRendering.cpp',
        'gpu/gl/GrGLProgram.cpp',
        'gpu/gl/GrGLProgramDataManager.cpp',
        'gpu/gl/GrGLRenderTarget.cpp',
        'gpu/gl/GrGLSemaphore.cpp',
        'gpu/gl/GrGLStencilAttachment.cpp',
        'gpu/gl/GrGLTestInterface.cpp',
        'gpu/gl/GrGLTexture.cpp',
        'gpu/gl/GrGLTextureRenderTarget.cpp',
        'gpu/gl/GrGLUtil.cpp',
        'gpu/gl/GrGLUniformHandler.cpp',
        'gpu/gl/GrGLVaryingHandler.cpp',
        'gpu/gl/GrGLVertexArray.cpp',

        'gpu/gl/builders/GrGLProgramBuilder.cpp',
        'gpu/gl/builders/GrGLShaderStringBuilder.cpp',

        'gpu/glsl/GrGLSL.cpp',
        'gpu/glsl/GrGLSLBlend.cpp',
        'gpu/glsl/GrGLSLFragmentProcessor.cpp',
        'gpu/glsl/GrGLSLFragmentShaderBuilder.cpp',
        'gpu/glsl/GrGLSLGeometryProcessor.cpp',
        'gpu/glsl/GrGLSLPrimitiveProcessor.cpp',
        'gpu/glsl/GrGLSLProgramBuilder.cpp',
        'gpu/glsl/GrGLSLProgramDataManager.cpp',
        'gpu/glsl/GrGLSLShaderBuilder.cpp',
        'gpu/glsl/GrGLSLUtil.cpp',
        'gpu/glsl/GrGLSLVarying.cpp',
        'gpu/glsl/GrGLSLVertexGeoBuilder.cpp',
        'gpu/glsl/GrGLSLXferProcessor.cpp',

        'gpu/mock/GrMockGpu.cpp',
        'gpu/ddl/GrDDLGpu.cpp',

        'gpu/SkGpuDevice.cpp',
        'gpu/SkGpuDevice_drawTexture.cpp',
        'gpu/SkGr.cpp',

        'image/SkImage_Gpu.cpp',
        'image/SkSurface_Gpu.cpp',

        # sksl
        'sksl/SkSLCFGGenerator.cpp',
        'sksl/SkSLCompiler.cpp',
        'sksl/SkSLCPPCodeGenerator.cpp',
        'sksl/SkSLGLSLCodeGenerator.cpp',
        'sksl/SkSLHCodeGenerator.cpp',
        'sksl/SkSLIRGenerator.cpp',
        'sksl/SkSLLexer.cpp',
        'sksl/SkSLLayoutLexer.cpp',
        'sksl/SkSLMetalCodeGenerator.cpp',
        'sksl/SkSLParser.cpp',
        'sksl/SkSLSPIRVCodeGenerator.cpp',
        'sksl/SkSLString.cpp',
        'sksl/SkSLUtil.cpp',
        'sksl/ir/SkSLSymbolTable.cpp',
        'sksl/ir/SkSLSetting.cpp',
        'sksl/ir/SkSLType.cpp',
    ]

    if platform & {'linux'}:
        cflags = fontconfig.cflags + freetype.cflags
        ldlibs = fontconfig.ldlibs + freetype.ldlibs

        srcs.extend([
            # gpu
            'gpu/gl/egl/GrGLMakeNativeInterface_egl.cpp',

            # fontmgr_fontconfig
            'ports/SkFontConfigInterface.cpp',
            'ports/SkFontConfigInterface_direct.cpp',
            'ports/SkFontConfigInterface_direct_factory.cpp',
            'ports/SkFontMgr_FontConfigInterface.cpp',
            'ports/SkFontMgr_fontconfig.cpp',
            'ports/SkFontMgr_fontconfig_factory.cpp',

            # freetype
            'ports/SkFontHost_FreeType.cpp',
            'ports/SkFontHost_FreeType_common.cpp',
        ])
    elif platform & {'macosx'}:
        cflags = ldlibs = []

        srcs.extend([
            # utils
            'utils/mac/SkCreateCGImageRef.cpp',
            'utils/mac/SkStream_mac.cpp',

            # gpu
            'gpu/gl/mac/GrGLMakeNativeInterface_mac.cpp',

            # fonts
            'ports/SkFontHost_mac.cpp',
        ])

    sources = skia_sources(*srcs)
    sources.append('deps/skia/third_party/gif/SkGifImageReader.cpp')

    public_includes = Path.glob('deps/skia/include/*')
    lib = cxx.build_lib('skia', sources, macros=['SK_ENABLE_DISCRETE_GPU'],
                        includes=Path.glob('deps/skia/src/*') + public_includes + \
                                 ['deps/skia/third_party/gif'], cflags=cflags)
    return Record(includes=public_includes + ['deps/skia/src/gpu'], lib=lib,
                  ldlibs=ldlibs)


def build(ctx):
    rec = configure(ctx)

    gl3w = build_gl3w(ctx, rec.c)
    abseil = build_abseil(ctx, rec.cxx)
    fmt = build_fmtlib(ctx, rec.cxx)
    tsm = build_libtsm(ctx, rec.c, rec.xkbcommon)
    skia = build_skia(ctx, rec.platform, rec.cxx, rec.freetype, rec.fontconfig)

    macros = ['UTERM_BLACK_SCREEN_WORKAROUND']
    if rec.xkbcommon is None:
        macros.append('USE_LIBTSM_XKBCOMMON')

    rec.cxx.build_exe('uterm', Path.glob('src/*.cc'),
                      includes=abseil.includes + gl3w.includes + skia.includes +
                               fmt.includes + tsm.includes +
                               ['deps/utfcpp/source', 'deps/concurrentqueue'],
                      libs=[abseil.base, abseil.strings, abseil.stacktrace, gl3w.lib,
                            skia.lib, fmt.lib, tsm.lib],
                      macros=macros,
                      external_libs=['dl', 'pthread'],
                      cflags=rec.glfw.cflags + rec.egl.cflags + rec.confuse.cflags,
                      ldlibs=rec.glfw.ldlibs + rec.egl.ldlibs + rec.confuse.ldlibs +
                             skia.ldlibs)
