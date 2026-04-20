using System;
using System.IO;
using System.Linq;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.CSharp.Syntax;

namespace RoslynPreprocessor
{
    class Program
    {
        static void Main(string[] args)
        {
            if (args.Length < 1) 
            {
                Console.WriteLine("Error: Missing input path");
                return; 
            }
            string inputPath = args[0];
            
            string code;
            try {
                code = File.ReadAllText(inputPath);
            } 
            catch (Exception ex) {
                Console.WriteLine($"Error reading {inputPath}: {ex.Message}");
                return;
            }

            var parseOptions = new CSharpParseOptions(
                preprocessorSymbols: new[] { "ANDROID", "MonoDroid" },
                languageVersion: LanguageVersion.Latest,
                documentationMode: DocumentationMode.Parse // Keep XML doc comments
            );
            
            var tree = CSharpSyntaxTree.ParseText(code, parseOptions);
            var root = tree.GetRoot();
            
            // Remove disabled #if blocks and directives
            var triviaToRemove = root.DescendantTrivia().Where(t => 
                t.IsKind(SyntaxKind.DisabledTextTrivia) ||
                t.IsKind(SyntaxKind.IfDirectiveTrivia) ||
                t.IsKind(SyntaxKind.ElifDirectiveTrivia) ||
                t.IsKind(SyntaxKind.ElseDirectiveTrivia) ||
                t.IsKind(SyntaxKind.EndIfDirectiveTrivia) ||
                t.IsKind(SyntaxKind.RegionDirectiveTrivia) ||
                t.IsKind(SyntaxKind.EndRegionDirectiveTrivia)
            );
            
            root = root.ReplaceTrivia(triviaToRemove, (o, r) => default(SyntaxTrivia));

            // Run rewriter to remove unapproved plugins/namespaces
            var rewriter = new CleanRewriter();
            root = rewriter.Visit(root);
            
            // We print to Console, Python reads stdout
            Console.WriteLine(root.ToFullString());
        }
    }
    
    class CleanRewriter : CSharpSyntaxRewriter
    {
        public override SyntaxNode VisitUsingDirective(UsingDirectiveSyntax node)
        {
            var name = node.Name.ToString();
            // Drop external namespaces. Keep System, Microsoft, and TCPOS.
            if (name.StartsWith("System") || name.StartsWith("Microsoft") || name.StartsWith("TCPOS"))
            {
                return base.VisitUsingDirective(node);
            }
            return null; // Remove this node completely
        }
    }
}
