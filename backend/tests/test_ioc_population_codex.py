"""IOC population and permissive-default regressions for W2-EXTRA-02/03.

The frozen worker report claimed a 7/158 source/output reconciliation but did
not ship a controlled affected-ID list or its executable census.  This module
keeps those claims testable without a sibling tree, live cache, network, or
writable project database:

* all seven permissive-default code paths are exercised with rejecting and
  accepting controls;
* the exact seven unitless storage occurrences are named and replayed through
  the production parser/observation path; and
* a compact, integrity-pinned extract from the supplied frozen release database
  independently joins canonical H/D source facts to emitted storage outputs.

The latter counterevidence is material: the frozen database actually contains
167 present storage observations (seven unitless), not the worker's stale 158.
The original 158 claim is retained explicitly and the nine-row difference is
asserted rather than silently rewriting history.
"""

from __future__ import annotations

import base64
import hashlib
import json
import pathlib
import sys
import tempfile
from types import SimpleNamespace
import unittest
import zlib

HERE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from adapters import ioc  # noqa: E402
from ferclib.registry import BY_ADAPTER  # noqa: E402
from ferclib.staging import Staging  # noqa: E402
from ferclib.status import Availability, Validation, VersionStatus  # noqa: E402


STORAGE_METRIC = next(
    metric for metric in BY_ADAPTER["ioc"]
    if metric.id == "ioc_contracted_storage_quantity"
)

# W2-EXTRA-02 described seven distinct permissive-default sites.  Items f and g
# are separate assertions because each can independently be stated, blank, or
# unrecognised in the same filing.
PERMISSIVE_DEFAULT_CONTROLS = {
    "header_item_f_unknown_is_not_blank": "test_header_units_keep_three_states",
    "header_item_g_unknown_is_not_blank": "test_header_units_keep_three_states",
    "missing_version_is_not_original": "test_missing_version_is_unresolved",
    "missing_template_is_not_interstate_gas": "test_missing_template_is_refused",
    "missing_retrieval_state_is_not_no_hits": "test_missing_retrieval_state_is_unverified",
    "population_rules_are_not_optional": "test_population_requires_both_rules",
    "unparseable_unit_rule_is_not_open": "test_unparseable_unit_rule_cannot_pass",
}

# Exact affected occurrences recovered independently from the frozen release's
# H facts, D facts, and observations.  Every row has one positive D item-p row
# totaling 75,000, header item f=T, and blank item g.
CHEYENNE_UNITLESS_STORAGE = (
    ("C000995", "20250102-5178", "2025-01-01", "T", "", 1, 75000.0),
    ("C000995", "20250401-5209", "2025-04-01", "T", "", 1, 75000.0),
    ("C000995", "20250701-5238", "2025-07-01", "T", "", 1, 75000.0),
    ("C000995", "20251001-5215", "2025-10-01", "T", "", 1, 75000.0),
    ("C000995", "20260102-5175", "2026-01-01", "T", "", 1, 75000.0),
    ("C000995", "20260401-5324", "2026-04-01", "T", "", 1, 75000.0),
    ("C000995", "20260701-5249", "2026-07-01", "T", "", 1, 75000.0),
)

WORKER_REPORTED_CANONICAL_HEADERS = 350
WORKER_REPORTED_BLANK_G_HEADERS = 140
WORKER_REPORTED_PRESENT_STORAGE = 158
WORKER_REPORTED_UNITLESS_STORAGE = 7

# zlib + RFC-1924 base85 encoding of canonical H/D population rows and present
# output rows selected from the 774,959,104-byte database in the supplied full
# archive (archive SHA-256 recorded inside).  Decompressed JSON is separately
# hashed, so corruption of this embedded adaptation fails closed.
FROZEN_POPULATION_JSON_SHA256 = (
    "1be5d1e95b9c9d26a61cc69625c7bc17cf542bc02d45dadf665b9afe284476b4"
)
FROZEN_POPULATION_B85 = b"""c-qaK+mal&4Tj%kuO$}`01v)Y@(8(0xwb~q$l8j$tJ+=J<+$?h>6s>5J<)UzNPwuMa-+2@f9gi#CqWSZUw5C6hmXhSm%E?;`Pbd?@%7>L&-;HJ|GfM8?r&!LpEGxF?mpdrc=~vpe)NyiFCM-;JpOwB@NxRJGY*V3?k)O|rhoS9`Zs=kdU<$#`2BeQ^7{0A`1N@I{Pf54pRqZ<xqEp$e0}-+^m_mC@OmtN&|71t|4;v7{r#^`uZJ&pKPU7yptC=j|Geo$gcG85fsY8I5aaZ>M;8qrF%Cks=7i|Mu@S8qgNSo1I?s(o92Sc>#p3i70W2R8Mj_6z7y=)0P%Ppci#GFL^r%=2B^DF?a|u@w!?0Kkb1b@sr?`m-qY&d1Luf$&u`?DeO#mHTT^zOvAbOLXu^hzni$?T(L>Ps5PCC|QK4Kh%SdvZvHsVIoVIvj+w2>Q&I4l;606Hsx2%`|svFL5&BMyqiDu9Xq6i3Bk5kL#v&m|0t#VUaG#n?6?j6$3jV^-iP_Qhg~GyyajiHN36{`$G*r8XKa7YK8eSL(Q2QI@N0@LXhaZDf?|To#D&<u23l;P9eg&EXXt6AOf~%0i0!w4y9l5iEy1EaBKHs}Uv^O25Rq1-F)oCHoTVs^hXU(2gbi66;FC0|zTeCy2VbxZHJhV1Y2!oFHOmVztE9ygR03`O6CE;4DwUI=@8kjbfIkV9hU)7h53y5=(-JRx!)p#;V)m;F;;v3S-UU13^)IC|GlBc~lf10#;o?WJU40ja9O|=Pd(+^h>N+9)o6<_vB)!)6DXoSRe%xG_$-X7DZhE%`ESUg-{1xGs}Bw@iS-5EbplW&&xx}(OKSGYL-u$S>96%n+`Oyyr&j3omJdoP_W7gp3xK^Pb^ygPw98K_#jPuY{;tOgT%GGO{!TQiEDWqRI@x1v%CwcSstm!T23{~BQeY8sG8-GnB~E%W_hG$`KX%Zk(lK}QqA&6J(`NDS)N)*!latz$%Uk(y2YRtk^oi3hgwJqPE~x!g(OfFA95iHRK<r}NXn|>LoFovs46~zDn6(xKIHlqsEQA{zNIQYh_o#40&4agP`KvDMefp<$@56uWH@&3NZZGV<Xr}@7os)OBXOHynLRBi9bagYNvDG!&*hShuT!q~Z_!|2atX&*s6A6Wt{@#>%b9iJa)B^cI=x43&s1Z+#{{&^1~{*{*`Q#}4d1~lZZ;@bb7O7N>`fwImBu%(*_(75t7wq7ny1zjtof`=wbDhwDmUo7;%0+@RTelw^VIq_)=7Wxhf?@x{h@G`^>ftJ9}3qTT|TSoPo@^TxNVhfuQ5}LUF@n72xFCt9h${10#?ZpM)N%VHdfsOCCy?N1*@ESMos<6#A25N&GU2uR*kKssXv)o?8=(w=@hIvZ@H{k?4n@JvE?+IxdOFi*BhgG@h4Dwc3sYzMYuq1+U0KgLHs?|)7Ye0gbUQRU6X=l5iZdCcCBU+E|42{9cUKe0=aY7WX&R6a6}O@DQa$l0=1=>`yPXId<B{KYSkg2jvxZ64msaAfhhT4jQgn{a9&m*vH@WbU~KzBw*liAz&VAO7`cF3^In*MwJH{wF^HpHaS;c4vfBn6^NOoVARBPdD_*V{Iv!vcm1e4LSjz^4L4aie?Ad@_F^EJZp{JR~Nj_lVbQPb$H}7nm=kmcIzWD|Fz~zf$_==2pWb$pK7%pGk@sLLhHBRs0^PS_*xWwgyL40%k1vcN9_**W}bNL3v-_nD|;|rtWuh!?h<?_KGzB&FNbNTw>kJzqY5_y^sHg1(%oaB9y;LKnw8i!;?*QRnd8|$oz9LAz^iO(2EF;=aMY{rdjQy7!&W?b~nF&Q@%PlB;%USxhVcq1NJjM3zlb;Ay09K~3x%$dzNE*`C&-qG|N1sF%gW7TNun2cfEH?!z;;4{K7#!?VTe8#?SCi&a@Dev;BE~j~Eh=LW|soU3RPM@J*Wp93H_8okTMZmJopBMdBv5(?5*0eJ4=VDA&?4zJyh4V|anm1M{SS7YJFAWi}rUZlRflEO;wnBM{s5ulT2kInA(7YN-!73fKnlr{ISaY|W(d?EZV3m$0?$c`e5^rNIS3{j*mZxChd1sI`r_WHZqQI4Ql#Z?1EoU|765PgGy5%&lhElLfmv8vL%JM;Ecxq#pLXL?lKANrC6s+<RHLnU$uu40vs`%W-ssk=TQ+#e>Exr$$TMP=;d?1~zruY!BN;@rRiqCDV>gk|)Rp>TW5E<DRq+@FlAI;Hc6fB9@Y89VEox4eQ07|W$4KAALo&3~KSbF66gfNP*tXU14Fb*QD8H7!^dA`gfTy{x`M~K7XaM3J2A&ep{cM%Pna8Ml9ZIojZj*7!@)l;OMFbs>sdJ~p@gpyl(9lQf?JcIDsjq9#w82bn>rbcXwo~NFQx;D)VK0*QpURl8;E*^}*D?2d5#fwAmYP&Ep@itNn6K~m(WfmR|ioL3G<f~P#P2|{k*DgHv;0=krWv3RIcw=I3*|G7y+%PEis<OGr#e*?;CH5Q_uPgR4Qti-!W*spjb-roTtRrTm))6&Z6$n^0gJ>G%+{Ri|i+C(!+e9nkcJnF)Zq6GfGvQzeTx@cAux*0HgsaC|$b;L6HXd9(+=?u?Fec`ffe^rigCTGww`C?=bIe(D?mk@L-ff#LTv}1T8>>?!u4`H3-B_U_ah0npp34<wxk?-PrBb~cD^&!pWjKUmaiMg4)t!|AE*A)Mm3CJ)mu!4tz1huukHYb_OlajjuOJ;?VQ$;Bms<XA?7<g_D_$8Q^KR_P*Te-h9dcgL^oN91ihqjRB@))Wq|ltuO2MjA%%a|QiGa17W<|ZW3li46#7T;#)+DTQeyV6{O~DfBCq?PlT6+Ezy@*IybKbIwJ%1#uvi){?Z5P&%?M}I6p0<}rwL4{c*0zcd)$Y_J&=eo4-RZRM(i9(x-Kjm@RRmS>q1v4qXEnu#Y<HTSrudNUPPty&!m(9Pkl4+2Rv4@FM1ZFFknK*<YKjjgQhNcCtxsAT>hLrWW2Z!T8?cNM#6kUZY`~iMnJqY*sUJ^Q=@@``wMMabRM{v3Xt{vUHjl?O_8IU2i}x<F0pl3J5`)Nof?F}j1YGK5o(C94#b6N)lgO_)w#mk+o0Sf@qaWmE#BIXZMVN>eXTb->N#7){S>vMSq;C?}e04T>#Yx{3uHtkAS#i=gfvZ{_IjcD7o5WRov@|Dulep&i0?kR^6s{6qY}-WP_&SMHY7r@!4NW56pas;lJ0fviGc)<e=q*i3guyfB<m`}mO6fNj8vrR-n||siEQWxAM+gH6i)NAu8;8gW5Ek`JY{EE*u<9sq3Aa||T*AdXkogR8SR5|eYS@G@iZEWeV%kMGC=M6B4P3%eaTtuV`3GzIcET_$4i^o!Y(f}Ccx@I!7hz`{W}@mr)ZHIus@5gl{b8mm9O&*3Gf~$9>h2FSQ7e4b-G^nWLZI$GEK_wM>F&ca@mwh--F;Z5YCzK6hh<{sM@`#vi{`eI9lCQ}pBMM8?L9Vd@#>U*7+2(S!5FT@<r)-UnaS0Zzvx_1)A6!}%WAH3C|u=IgQnvpg{wB5YdT(%xSWe8E=|Wv0@remtGfQ8aFvcRqq+Vfa8<`k)Leg2xaLvR5jEFex4EiwSyHWoQ@Be0(p`ULVyC1<@_?Pz;T*{u3>)Sn6u^>T>1GeD<2jgswRNA|_+eWIbg%*EluKUv+tv{s>EsO)8GywzG_e8W7{C&P<ilIrI;MjOxU|kNKa%Z19c;j2C`pb%(>khS+S;OJ0M7ZDydJlvby&w9;Pj0Xt%)Pqm<JmDwk(+JHd}irgmdX5lOJKDH?aU=xi=KpgmDmIJq~0OZe$uZA=ZX&$0NjHaaawM>2_s}dM7|w;t&Q29-pvYlk|fWk4;z$z#$HjJORS_jsdx++BVMoaMp(qJcqD#4sGM$kDG)@RXx7n{f0wXG6sy{5SDw+c~Faqv)1zni>5@!>+RomZ%pVjhj7sp@|O}Dy&EwkCgH3pWYzPHb*pKK>X?M(TXwv5v2EYR;2^LFtEK?&63n)1V+e^wxM&LU-tx8|V#vYr2#coheeXBoFaa{3;kCQ@dxX&Fg&33FCUhnTs(lJIV`tmyT%1kzE+PXF+WN(|5n&i&nbP9fh;bBR^@$B!#H~dv8*!QY!9>Jyu~^4YIqK(<LgRKy0I|fPWh0J?#pQZ7`za2K#l;Rd@DRheSghSOmYQ@@V~3>#;;ethvJtyt(fdd&)FBzMQCw^^X8V#L39uYW0T(b10<3pwxleFwCCde@x?&6qaC<|=2Ly>Hh{IxVIh0~Q!9g)t52fg5K8?F6E?_;BO1!T)42!{fDCH9u5Jmx(rh&)>?217!#IATx2ZS~$$$K~UNr16!3$P6s#{kx&W%d)?_>8%LOM3$oa8pY7fY3JA!ZzOin*RP=!ZMY?vI*lL!WxB+OSmN_T*A6JF-*d3HQ^JM76)dk*_IP)xrC+1!LkWs2O%I*osE6cn{r6PD&;t<H|3CoHE%Vu(VKEe!K#iVPH)N~0jmmGa(Yt^Nm$oTiO2Rsld#H7(Wp1&kb+fX%j-=!Bw*EpBV+ZZ9Fnlgh+RczV-i-pV%sIy3jNEQyQgojU%$Oh|MEYt7^0Z7pV>yM!TAQ$!{f{A;qkhte0zL&oqqQB*U!^0em{J9n10j4)8q7`uZNeH`Dfpb_mAIxI~Di54d^!7{<c8ioT$!M@vnk_7D%z>G+Q8138Z$#xebu|IgbXx_4?#T5LAnir60j=fq)jss-^NOh*FGHWyo8vMk_{^o5|OMHK@hN(kqa*KtKy5PEk+{J+6X`h!MNJz1YsI9j^xcsZG!}f|QB6UNFV?fyD2JyHY8>3#5F$=ayKh6yF1aYqv3}6yFCjf4(*druaUPc{Ihj0w|T@yFlvVKM1Dy9*|m$0uW5`eIRp;Y%BwUT8z{sy#>J(e*+}I&MF>^<SIV-F#WyTIOeU_*;HI?5#Pg6jxi+@d=E#xCm$u3@a-IR;k>b@L9O?)+&7m@sy!Sfr(%>`Hn(%sl<GxS%{?6O{j5f8(cHr^$4B;(Yvz3p+|H-l-)+x(0Snv51X~=%>Roch+{F>UJ2^<@(;klMD1n0cw1cA>M{KOyQ7b+cv!EoH*Sk34cb^KWyxzl6_fwMPkPeQT*Ig3K>s=i4ddy4a^)8P1eVm0<Ucbcw+j%{KU|x4|60AvJavMp>?2;>Gn~inW#72^m+mm2!?<1+%U2?TtNRn+KS@OFR%<r8fCBsL-4Btr-P2SbKs6|q5xMaaB-$`N()VW?)Np8l;%hR{#56An%<I``4FMr-YAAf&1{vm+2+-s2BT6B^W5ekAL)JamdpOPi{TO>K{)a18I;av*^7tQpv+dxXiMs|nM1G2i6FSo_$0IBs>Bf7_E2U%{(CDBbr4+!iZGHS(0J$Cg*bequuQZ98nBf8IM2dT%d8>bXOEk+h=(IC3h@D9B3Z6Kw6!;5Y;Zh+uUSEuA&Blrjj8aYZEXA;ctT^yyiPBOvwaMaqbWDTi<V>xoog6X}B1NP4smExl&cPH6X)5B5qU-E3AgQKSQ4UGv}@li9oi-MWGi=)Jc6HM$kIP%_iOI()yFU0A`_kSFoAE!U`5+>qyIco!Del;f3KHZ@xWobsN0FOi|i!+O~4|pI--GoP^eZxah=4F5b+GjizrK|*q)nE~n>La{k@-EhrM)FPHF>@DdN$UYwo3@L!q>+r;cg)$vTGDI}2;4Z_0xgi0*}7OuS`WzDR9&nktpnu3JYB3MtqWvjk}lSgMlwd<@j@4CNxS8Lo^Dvp{s`jxUJc0#aK}+rto^wQWMv#HrZrt4D}Puqt?2<-+rWxxO$W$@+pCz?bb+i)UB$Gf3uNWpDyB6(AZv?OF|FwUxo~0?)0!@jmEo$G)^vfad{xD?W*?+@8tmA)h?N#81|&PKEn=m`c8&{k7O~P|568-bMXa<)G3D5?S`jNPc5tj6Rm4h*?Hm`zDPpC?9*&hiid<=t;EA$hgCbX2Bv_y9xSfc#r;#T5@0glMEmiwKb}!oYW%e$RwMB_kYVQHLa3Yb)?R_9C!x5?E-UqVs6_LvBT_9`w5UKRu19IUKB9-6!KvrX3q!K*M0A$DWLn_15d_H!pJ)}}R&CX-T!9y;yQd~QBj5@@+eo5vWJANEuO}``$jvd<#v7TR&)y9syhFHrl$x&m+L_@6Omt>r=<CP)SRZH^6*s;VAYpNyLVC*<wi1pNx+%9$uF68cZkS3$=_*jUop8H5vb`@etzKdk-N+Gs>?jgA_pAcI>_mRNf7!uTrlM5>eu_bgD$=WeOYz^H*a$yW1wutT{S@}PRt)lx#RyGe}%V?Ul!;YJS*!?-u=<*%c2C?gNJs@j$2C;Q?2grpBgV;K{3k3F_2&%=%g{y+tI=TmB?VcdEj_v>n`xkM8T8vz{A&9M`yFga12XgCZiq*l6yMa_@C5^k^F)@%ztUVlSuL7yO+QD&QNg$P0yEtIK`K#-TA<d!RF&Gfbsy!fU9|5tf+5vK57a*2ZyFga10Ag8{<OHxY{9i1qeh2yG&E3m~&&S^m({Fru`tbJC^V7eNk8hukUyg^D<J+%KU%!1h|3PnmKhJC~m%X1qd_E6+{(Nv4PAC|n1IRudVgP)9u%C`5zfTrY`0&ZcWIlYf(?81ZgME+r@acGTcnIF)Pd>Si`2CNbun+I2|MdO)KVOe8cR$An8M8I;pZ@}uTVVA"""


class _SilentCtx:
    def __init__(self, staging=None):
        self.staging = staging
        self.messages = []

    def log(self, level, message, **_kwargs):
        self.messages.append((level, message))


def _ioc_bytes(*, cid: str, snapshot: str, transport_code: str = "T",
               storage_code: str = "", storage_quantity: str = "75000",
               name: str = "Cheyenne Plains Gas Pipeline Company, L.L.C.") -> bytes:
    """One labelled synthetic IOC file exercising the real positional parser."""
    header = [
        "H", name, cid, snapshot, "O", snapshot,
        transport_code, storage_code, "AUDIT SYNTHETIC CONTACT", "",
    ]
    detail = [
        "D", "AUDIT SYNTHETIC CUSTOMER", "123", "N", "FT", "SYNTHETIC-C1",
        "2020-01-01", "2029-12-31", "", "N", "1000", storage_quantity, "",
    ]
    return ("\t".join(header) + "\n" + "\t".join(detail) + "\n").encode("utf-8")


_ABSENT = object()


def _emit_storage(*, cid: str, accession: str, snapshot: str,
                  transport_code: str = "T", storage_code: str = "",
                  storage_quantity: str = "75000", version_status=_ABSENT):
    raw = _ioc_bytes(
        cid=cid, snapshot=snapshot, transport_code=transport_code,
        storage_code=storage_code, storage_quantity=storage_quantity)
    parsed = ioc.parse_ioc(raw, expect_cid=cid, accession=accession)
    month = int(snapshot[5:7])
    filing = {
        "_parsed": parsed,
        "reporting_year": int(snapshot[:4]),
        "reporting_period": ioc.QUARTER_OF_MONTH[month],
        "filing_id": accession,
        "filed_date": snapshot,
        "_document_id": f"AUDIT_SYNTHETIC_DOCUMENT:{accession}",
    }
    if version_status is not _ABSENT:
        filing["version_status"] = version_status
    observations = ioc._snapshot_observations(
        _SilentCtx(), {"entity_key": cid}, filing,
        {STORAGE_METRIC.id: STORAGE_METRIC}, ioc._Lineage(cid))
    matches = [o for o in observations if o["metric_id"] == STORAGE_METRIC.id]
    if len(matches) != 1:
        raise AssertionError(
            f"fixture {accession} emitted {len(matches)} storage observations")
    return parsed, matches[0]


def _frozen_population():
    raw = zlib.decompress(base64.b85decode(FROZEN_POPULATION_B85))
    actual = hashlib.sha256(raw).hexdigest()
    if actual != FROZEN_POPULATION_JSON_SHA256:
        raise AssertionError(
            f"embedded IOC population fixture hash mismatch: {actual}")
    payload = json.loads(raw)
    if payload.get("schema") != "ioc-frozen-release-population-v1":
        raise AssertionError("embedded IOC population fixture has the wrong schema")
    return payload


class TestSevenPermissiveDefaults(unittest.TestCase):

    def test_control_inventory_is_exact_and_callable(self):
        self.assertEqual(len(PERMISSIVE_DEFAULT_CONTROLS), 7)
        self.assertEqual(len(set(PERMISSIVE_DEFAULT_CONTROLS)), 7)
        for control_id, method_name in PERMISSIVE_DEFAULT_CONTROLS.items():
            with self.subTest(control=control_id):
                self.assertTrue(callable(getattr(self, method_name, None)))

    def test_header_units_keep_three_states(self):
        base = ["H", "Pipeline", "C000995", "2025-01-01", "O", "2025-01-01",
                "T", "B", "contact", ""]
        for index, prefix in ((6, "uom_transport"), (7, "uom_storage")):
            with self.subTest(item=("f" if index == 6 else "g")):
                row = list(base)
                row[index] = "X"
                unknown = ioc._read_header(row)
                row[index] = ""
                blank = ioc._read_header(row)
                row[index] = "T"
                stated = ioc._read_header(row)
                self.assertEqual(unknown[prefix], "")
                self.assertEqual(unknown[prefix + "_state"], "unrecognised")
                self.assertEqual(blank[prefix], "")
                self.assertEqual(blank[prefix + "_state"], "blank")
                self.assertEqual(stated[prefix], "Dth")
                self.assertEqual(stated[prefix + "_state"], "stated")

    def test_missing_version_is_unresolved(self):
        _parsed, missing = _emit_storage(
            cid="C000995", accession="AUDIT_SYNTHETIC_NO_VERSION",
            snapshot="2025-01-01", storage_code="T")
        self.assertEqual(missing["version_status"], VersionStatus.UNRESOLVED)

        _parsed, stated = _emit_storage(
            cid="C000995", accession="AUDIT_SYNTHETIC_REVISED",
            snapshot="2025-04-01", storage_code="T",
            version_status=VersionStatus.REVISED)
        self.assertEqual(stated["version_status"], VersionStatus.REVISED)

    def test_missing_template_is_refused(self):
        with self.assertRaisesRegex(ValueError, "no template"):
            ioc._template_of({"entity_key": "AUDIT_SYNTHETIC"}, [])
        self.assertEqual(
            ioc._template_of({"template": "gas_storage"}, []), "gas_storage")
        self.assertEqual(
            ioc._template_of({}, [{"template": "interstate_gas"}]),
            "interstate_gas")

    def test_missing_retrieval_state_is_unverified(self):
        entity_key = "AUDIT_SYNTHETIC_UNRECORDED_RETRIEVAL"
        slot = {
            "metric_id": STORAGE_METRIC.id,
            "reporting_year": 2025,
            "reporting_period": "Q1",
            "instant_date": "2025-01-01",
            "requirement_evidence": "AUDIT SYNTHETIC requirement",
        }
        prior = ioc._RETRIEVAL_STATE.pop(entity_key, None)
        try:
            missing = ioc._account_for_remainder(
                {"entity_key": entity_key}, {STORAGE_METRIC.id: STORAGE_METRIC},
                [slot], [], [])[0]
            self.assertEqual(
                missing["availability"], Availability.UNVERIFIED_AVAILABILITY)
            self.assertIn("no retrieval outcome was recorded", missing["missing_reason"])
            self.assertIn("NOT a finding", missing["missing_reason"])

            ioc._RETRIEVAL_STATE[entity_key] = (
                "search_failed", "AUDIT SYNTHETIC observed search failure")
            failed = ioc._account_for_remainder(
                {"entity_key": entity_key}, {STORAGE_METRIC.id: STORAGE_METRIC},
                [slot], [], [])[0]
            self.assertEqual(failed["availability"], Availability.RETRIEVAL_FAILED)
            self.assertIn("observed search failure", failed["missing_reason"])
        finally:
            if prior is None:
                ioc._RETRIEVAL_STATE.pop(entity_key, None)
            else:
                ioc._RETRIEVAL_STATE[entity_key] = prior

    def test_population_requires_both_rules(self):
        obs = {"observation_id": "AUDIT_SYNTHETIC_OBSERVATION"}
        kwargs = dict(
            filing_ids=["AUDIT_SYNTHETIC_FILING"],
            members=["AUDIT_SYNTHETIC_FILING:D00002"],
            candidate_count=1,
        )
        for inclusion, exclusion in (("", "exclude none"), ("include D", "")):
            with self.subTest(inclusion=inclusion, exclusion=exclusion):
                with self.assertRaisesRegex(ValueError, "inclusion or exclusion"):
                    ioc._population(
                        obs, "AUDIT_SYNTHETIC_POPULATION",
                        inclusion=inclusion, exclusion=exclusion, **kwargs)
        valid = ioc._population(
            obs, "AUDIT_SYNTHETIC_POPULATION",
            inclusion="include the named D row", exclusion="exclude no D row", **kwargs)
        self.assertEqual(valid["row_count"], 1)
        self.assertTrue(valid["inclusion_rule"])
        self.assertTrue(valid["exclusion_rule"])

    def test_unparseable_unit_rule_cannot_pass(self):
        bad_metric = SimpleNamespace(
            id="AUDIT_SYNTHETIC_UNPARSEABLE", unit_rule="by weight",
            canonical_unit="", admissible_units=(), admissible_units_evidence="")
        row = {"unit": "Dth", "qa_flags": "", "validation": Validation.PASS}
        got = ioc._flag_unit_family(row, bad_metric)
        self.assertEqual(ioc.unit_rule_state(bad_metric), "unparseable")
        self.assertEqual(got["validation"], Validation.NOT_YET_VALIDATED)
        self.assertIn("UNIT NOT CHECKED", got["qa_flags"])

        good_metric = SimpleNamespace(
            id="AUDIT_SYNTHETIC_RESOLVED", unit_rule="Dth",
            canonical_unit="Dth", admissible_units=(), admissible_units_evidence="")
        control = {"unit": "Dth", "qa_flags": "", "validation": Validation.PASS}
        got = ioc._flag_unit_family(control, good_metric)
        self.assertEqual(ioc.unit_rule_state(good_metric), "resolved")
        self.assertEqual(got["validation"], Validation.PASS)
        self.assertEqual(got["qa_flags"], "")


class TestIOCStorageUnitPopulation(unittest.TestCase):

    def test_frozen_source_and_output_populations_reconcile(self):
        payload = _frozen_population()
        self.assertEqual(
            payload["source_archive_sha256"],
            "2567244938a7645ba1fe09b81486cf3780cd1a769b61b59afee25a6309f382d5")
        self.assertEqual(payload["source_database_bytes"], 774959104)

        headers = payload["headers"]
        outputs = payload["outputs"]
        source = {
            (row["entity_key"], row["filing_id"], row["snapshot_date"]): row
            for row in headers if row["positive_storage_rows"] > 0
        }
        emitted = {
            (row["entity_key"], row["filing_id"], row["instant_date"]): row
            for row in outputs
        }
        source_unitless = {key for key, row in source.items() if not row["g_code"]}
        emitted_unitless = {key for key, row in emitted.items() if not row["unit"]}
        expected_unitless = {(r[0], r[1], r[2]) for r in CHEYENNE_UNITLESS_STORAGE}

        self.assertEqual(len(headers), 382)
        self.assertEqual(sum(not row["g_code"] for row in headers), 146)
        self.assertEqual(len(source), 167)
        self.assertEqual(len(emitted), 167)
        self.assertEqual(set(source), set(emitted))
        self.assertEqual(source_unitless, expected_unitless)
        self.assertEqual(emitted_unitless, expected_unitless)
        for key in expected_unitless:
            self.assertEqual(source[key]["positive_storage_rows"], 1)
            self.assertEqual(source[key]["storage_total"], 75000.0)
            self.assertEqual(emitted[key]["value_num"], 75000.0)
            self.assertEqual(emitted[key]["validation"], Validation.UNIT_WARNING)

        # Preserve and falsify the stale worker population explicitly.  The
        # seven-row numerator survived; the denominator grew by nine, while
        # canonical and blank-header populations grew by 32 and six.
        self.assertEqual(len(source_unitless), WORKER_REPORTED_UNITLESS_STORAGE)
        self.assertNotEqual(len(source), WORKER_REPORTED_PRESENT_STORAGE)
        self.assertEqual(len(source) - WORKER_REPORTED_PRESENT_STORAGE, 9)
        self.assertEqual(len(headers) - WORKER_REPORTED_CANONICAL_HEADERS, 32)
        self.assertEqual(
            sum(not row["g_code"] for row in headers)
            - WORKER_REPORTED_BLANK_G_HEADERS, 6)

    def test_exact_seven_replay_as_unitless_warnings(self):
        seen = set()
        for cid, accession, snapshot, f_code, g_code, positive_rows, total in (
                CHEYENNE_UNITLESS_STORAGE):
            with self.subTest(accession=accession):
                parsed, observation = _emit_storage(
                    cid=cid, accession=accession, snapshot=snapshot,
                    transport_code=f_code, storage_code=g_code,
                    storage_quantity=str(total), version_status=VersionStatus.ORIGINAL)
                positive = [c for c in parsed["contracts"]
                            if (c["storage_quantity"] or 0) > 0]
                self.assertEqual(len(positive), positive_rows)
                self.assertEqual(parsed["header"]["uom_transport_state"], "stated")
                self.assertEqual(parsed["header"]["uom_storage_state"], "blank")
                self.assertEqual(observation["availability"], Availability.PRESENT)
                self.assertIsNone(observation["unit"])
                self.assertEqual(observation["validation"], Validation.UNIT_WARNING)
                self.assertEqual(observation["value_num"], total)
                self.assertIn("g=blank -> not stated", observation["qa_flags"])
                seen.add((cid, accession, snapshot))
        self.assertEqual(
            seen, {(r[0], r[1], r[2]) for r in CHEYENNE_UNITLESS_STORAGE})

    def test_stated_unit_and_no_storage_are_valid_controls(self):
        _parsed, stated = _emit_storage(
            cid="C000995", accession="AUDIT_SYNTHETIC_STATED_G",
            snapshot="2025-01-01", storage_code="T",
            version_status=VersionStatus.ORIGINAL)
        self.assertEqual(stated["availability"], Availability.PRESENT)
        self.assertEqual(stated["unit"], "Dth")
        self.assertEqual(stated["validation"], Validation.PASS)

        _parsed, absent = _emit_storage(
            cid="C000995", accession="AUDIT_SYNTHETIC_NO_STORAGE",
            snapshot="2025-04-01", storage_code="",
            storage_quantity="0", version_status=VersionStatus.ORIGINAL)
        self.assertEqual(absent["availability"], Availability.NOT_APPLICABLE)
        self.assertIsNone(absent["value_num"])
        self.assertIsNone(absent["unit"])
        self.assertIn("no contract", absent["missing_reason"])


class TestIOCReplayOrderSafety(unittest.TestCase):
    DOCS = (
        ("AUDIT_SYNTHETIC_MW_1", "C001087", "MountainWest Pipeline, LLC"),
        ("AUDIT_SYNTHETIC_OT_1", "C001088",
         "MountainWest Overthrust Pipeline, LLC"),
    )

    def _run_order(self, order):
        with tempfile.TemporaryDirectory(prefix="ioc-population-order-") as tmp:
            staging = Staging(pathlib.Path(tmp) / "ioc.sqlite")
            staging.start_run("test", {}, "AUDIT_SYNTHETIC_REGISTRY", "AUDIT_SYNTHETIC_CODE")
            ctx = _SilentCtx(staging)
            observed = []
            try:
                legal_names = {cid: name for _acc, cid, name in self.DOCS}
                staging.upsert("entities", [{
                    "entity_key": cid, "cid": cid, "legal_name": legal_names[cid],
                } for cid in sorted(legal_names)], ["entity_key"])
                for cid in order:
                    entity = {
                        "entity_key": cid,
                        "legal_name": legal_names[cid],
                        "template": "interstate_gas",
                        "assets": [{
                            "asset_id": f"AUDIT_SYNTHETIC_ASSET_{cid}",
                            "template": "interstate_gas",
                        }],
                    }
                    accepted = []
                    for accession, header_cid, header_name in self.DOCS:
                        raw = _ioc_bytes(
                            cid=header_cid, snapshot="2025-01-01",
                            storage_code="T", name=header_name)
                        before = staging.query("SELECT COUNT(*) AS n FROM filings")[0]["n"]
                        try:
                            parsed = ioc.parse_ioc(
                                raw, expect_cid=cid, accession=accession)
                        except ioc.IOCEntityMismatch:
                            after = staging.query(
                                "SELECT COUNT(*) AS n FROM filings")[0]["n"]
                            self.assertEqual(before, after, "a rejected CID wrote a filing")
                            continue
                        candidate = {
                            "accession": accession,
                            "filed_date": "2025-01-02",
                            "posted_date": "2025-01-02",
                            "issued_date": "",
                            "avail_code": "P",
                            "data_files": [{
                                "fileId": accession + "_FILE",
                                "fileName": accession + ".tab",
                            }],
                        }
                        bundle = {
                            "parsed": parsed,
                            "data_name": accession + ".tab",
                            "data_bytes": raw,
                            "entry": {
                                "last_seen_at": "2026-09-09T00:00:00+00:00",
                                "first_seen_at": "2026-09-09T00:00:00+00:00",
                                "cache_path": "AUDIT_SYNTHETIC_CACHE/" + accession,
                            },
                            "listing": [],
                        }
                        accepted.append(ioc._persist(ctx, entity, candidate, bundle))
                    self.assertEqual(len(accepted), 1)
                    ioc._mark_canonical(ctx, entity, accepted)
                    observations, _edges = ioc.canonicalise(ctx, entity, accepted, [])
                    observed.extend(
                        (row["entity_key"], row["filing_id"])
                        for row in observations if row.get("filing_id"))
                    ioc._PENDING_POPULATIONS.pop(cid, None)
                    ioc._PENDING_EVENTS.pop(cid, None)

                ownership = [tuple(row) for row in staging.query(
                    "SELECT f.filing_id,f.entity_key,"
                    "json_extract(sf.typed_dims_json,'$.pipeline_id_normalised') "
                    "FROM filings f JOIN source_facts sf "
                    "ON sf.source_system=f.source_system AND sf.filing_id=f.filing_id "
                    "AND sf.concept_local='ioc_header_record' ORDER BY f.filing_id")]
                return ownership, sorted(set(observed))
            finally:
                ioc._TABLE_CACHE.pop(id(staging), None)
                staging.close()

    def test_mountainwest_overthrust_is_order_independent(self):
        forward = self._run_order(("C001087", "C001088"))
        reverse = self._run_order(("C001088", "C001087"))
        expected_ownership = [
            ("AUDIT_SYNTHETIC_MW_1", "C001087", "C001087"),
            ("AUDIT_SYNTHETIC_OT_1", "C001088", "C001088"),
        ]
        expected_observations = [
            ("C001087", "AUDIT_SYNTHETIC_MW_1"),
            ("C001088", "AUDIT_SYNTHETIC_OT_1"),
        ]
        self.assertEqual(forward, reverse)
        self.assertEqual(forward[0], expected_ownership)
        self.assertEqual(forward[1], expected_observations)


if __name__ == "__main__":
    unittest.main()
